from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.config import (
    get_embedding_settings,
    get_manticore_settings,
    get_sqlite_settings,
)
from app.db import db_connection
from app.embeddings import UnifiedEmbeddingClient
from app.indexing_state import (
    chunk_set_hash,
    ensure_active_generation,
    get_pending_deleted_chunk_ids,
    get_pending_video_outbox_event_ids,
    mark_outbox_events_completed,
)
from app.manticore import date_to_int, get_manticore_client
from app.pipeline.base import PipelineStage, StageResult
from app.repository import update_video_status

logger = logging.getLogger("app.pipeline.index")


class IndexStage(PipelineStage):
    """Stage 3: Расчет эмбеддингов для чанков и загрузка в Manticore Search."""

    def stage_name(self) -> str:
        return "stage_3_index"

    async def execute(
        self,
        task_id: int,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> StageResult:
        video_id = payload["video_id"]
        title = payload.get("title", f"Video {video_id}")
        logger.info(f"Индексация: {title}")

        if progress_callback:
            progress_callback({"active": True, "title": title, "progress": 10, "status_text": "Эмбеддинги"})

        settings = get_sqlite_settings()
        q_settings = get_manticore_settings()
        embed_client = UnifiedEmbeddingClient(get_embedding_settings())
        manticore = get_manticore_client()

        with db_connection(settings) as conn:
            generation_id = ensure_active_generation(conn)
            payload_generation = payload.get("generation_id")
            if payload_generation is not None and int(payload_generation) != generation_id:
                generation_error = (
                    f"Index task generation {payload_generation} is stale; active generation is {generation_id}"
                )
                return StageResult(
                    success=False,
                    error=generation_error,
                )
            update_video_status(conn, video_id=video_id, status="indexing")
            sql_v = "SELECT title, source_file_id, source_url, recorded_date, is_short, is_4k FROM videos WHERE id = ?"
            v_row = conn.execute(sql_v, (video_id,)).fetchone()
            sql_c = """
                SELECT id, chunk_index, text, start_sec, end_sec FROM chunks
                WHERE video_id = ? ORDER BY chunk_index ASC
            """
            chunks = conn.execute(sql_c, (video_id,)).fetchall()
            snapshot_hash = chunk_set_hash(conn, video_id)
            outbox_event_ids = get_pending_video_outbox_event_ids(conn, video_id, generation_id)
            deleted_chunk_ids = get_pending_deleted_chunk_ids(conn, video_id, generation_id)

        if not chunks:
            with db_connection(settings) as conn:
                if chunk_set_hash(conn, video_id) != snapshot_hash:
                    update_video_status(conn, video_id=video_id, status="transcribed")
                    return StageResult(success=True)
            if deleted_chunk_ids:
                await asyncio.to_thread(
                    manticore.delete,
                    q_settings.table_name,
                    deleted_chunk_ids,
                )
            with db_connection(settings) as conn:
                mark_outbox_events_completed(conn, outbox_event_ids)
                current_status = (
                    "indexed_chunks_ready" if chunk_set_hash(conn, video_id) == snapshot_hash else "transcribed"
                )
                update_video_status(conn, video_id=video_id, status=current_status)
            return StageResult(success=True)

        texts = [c["text"] for c in chunks]

        def embedding_progress(current: int, total: int) -> None:
            pct = 10 + int(current / total * 65) if total > 0 else 10
            if progress_callback:
                progress_callback({"progress": pct, "status_text": f"Эмбеддинги: {current}/{total}"})

        # 1. Вычисление эмбеддингов
        embeddings_data = await embed_client.embed_batch_async(texts, progress_callback=embedding_progress)

        with db_connection(settings) as conn:
            if chunk_set_hash(conn, video_id) != snapshot_hash:
                logger.info("Chunk set for video %s changed during embedding; skipping stale index write", video_id)
                update_video_status(conn, video_id=video_id, status="transcribed")
                return StageResult(success=True)

        if progress_callback:
            progress_callback({"progress": 75, "status_text": "Загрузка в Manticore"})

        # 2. Подготовка точек (points) для Manticore
        points = []
        for idx, row in enumerate(chunks):
            dense_vec, sparse_vec = embeddings_data[idx]
            vd: dict[str, Any] = {"default": dense_vec}
            # Sparse vectors are computed and cached in SQLite, but Manticore is structured
            # to do hybrid search via dense KNN + full-text MATCH (BM25), so text-sparse is
            # currently kept in payload/cache for potential future migration but not indexed in Manticore.
            if sparse_vec:
                vd["text-sparse"] = sparse_vec

            points.append(
                {
                    "id": row["id"],
                    "vector": vd,
                    "payload": {
                        "chunk_id": row["id"],
                        "chunk_index": row["chunk_index"],
                        "text": row["text"],
                        "start_sec": row["start_sec"],
                        "end_sec": row["end_sec"],
                        "video_id": str(video_id),
                        "title": v_row["title"],
                        "recorded_date": date_to_int(v_row["recorded_date"]),
                        "is_short": bool(v_row["is_short"]),
                        "is_4k": bool(v_row["is_4k"]),
                        "source_file_id": v_row["source_file_id"],
                        "source_url": v_row["source_url"],
                        "is_primary": True,
                    },
                }
            )

        # 3. Синхронная отправка в Manticore (выполняется в thread pool)
        loop = asyncio.get_running_loop()
        if points:
            await loop.run_in_executor(
                None, lambda p=points: manticore.upsert(collection_name=q_settings.table_name, points=p)
            )

        # Old documents are removed only after every replacement has been accepted.
        # If this stage fails before this point, the searchable index remains intact.
        if deleted_chunk_ids:
            await loop.run_in_executor(
                None,
                lambda ids=deleted_chunk_ids: manticore.delete(
                    collection_name=q_settings.table_name,
                    ids=ids,
                ),
            )

        # 4. Обновление статуса в БД
        with db_connection(settings) as conn:
            mark_outbox_events_completed(conn, outbox_event_ids)
            conn.execute(
                """
                UPDATE index_generations
                SET indexed_chunks = (
                    SELECT COUNT(DISTINCT chunk_id) FROM index_outbox
                    WHERE generation_id = ? AND event_type = 'upsert' AND status = 'completed'
                )
                WHERE id = ?
                """,
                (generation_id, generation_id),
            )
            current_status = (
                "indexed_chunks_ready" if chunk_set_hash(conn, video_id) == snapshot_hash else "transcribed"
            )
            update_video_status(conn, video_id=video_id, status=current_status)

        logger.info(f"{v_row['title']} доступен для поиска.")
        return StageResult(success=True)
