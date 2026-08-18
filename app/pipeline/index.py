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
            update_video_status(conn, video_id=video_id, status="indexing")
            sql_v = "SELECT title, source_file_id, source_url, recorded_date, is_short, is_4k FROM videos WHERE id = ?"
            v_row = conn.execute(sql_v, (video_id,)).fetchone()
            sql_c = """
                SELECT id, chunk_index, text, start_sec, end_sec FROM chunks
                WHERE video_id = ? ORDER BY chunk_index ASC
            """
            chunks = conn.execute(sql_c, (video_id,)).fetchall()

        if not chunks:
            with db_connection(settings) as conn:
                update_video_status(conn, video_id=video_id, status="indexed_chunks_ready")
            return StageResult(success=True)

        texts = [c["text"] for c in chunks]

        def embedding_progress(current: int, total: int) -> None:
            pct = 10 + int(current / total * 65) if total > 0 else 10
            if progress_callback:
                progress_callback({"progress": pct, "status_text": f"Эмбеддинги: {current}/{total}"})

        # 1. Вычисление эмбеддингов
        embeddings_data = await embed_client.embed_batch_async(texts, progress_callback=embedding_progress)

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

        # 4. Обновление статуса в БД
        with db_connection(settings) as conn:
            update_video_status(conn, video_id=video_id, status="indexed_chunks_ready")

        logger.info(f"{v_row['title']} доступен для поиска.")
        return StageResult(success=True)
