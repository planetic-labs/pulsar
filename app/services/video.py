from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from app.chunking import chunk_from_utterances
from app.database import Database
from app.ports import EmbeddingPort, VectorStorePort
from app.repos.chunk_repo import ChunkRepository
from app.repos.task_repo import TaskRepository
from app.repos.video_repo import VideoRepository
from app.settings import Settings

logger = logging.getLogger("app.services.video")


class VideoNotFoundError(Exception):
    pass


class VideoProcessingError(Exception):
    pass


class VideoService:
    """Сервисный слой для управления бизнес-логикой видеозаписей."""

    def __init__(
        self,
        db: Database,
        video_repo: VideoRepository,
        chunk_repo: ChunkRepository,
        task_repo: TaskRepository,
        manticore: VectorStorePort,
        embedder: EmbeddingPort,
        settings: Settings,
    ) -> None:
        self.db = db
        self.video_repo = video_repo
        self.chunk_repo = chunk_repo
        self.task_repo = task_repo
        self.manticore = manticore
        self.embedder = embedder
        self.settings = settings

    async def get_video_details(self, video_id: int) -> dict[str, Any]:
        """Возвращает детальную информацию о видео, включая папки и количество чанков."""
        sql = """
            SELECT v.*, f.name as folder_name, orig.title as original_title,
                   (SELECT COUNT(*) FROM chunks WHERE video_id = v.id) as chunk_count
            FROM videos v
            LEFT JOIN folders f ON v.parent_folder_id = f.id
            LEFT JOIN videos orig ON v.original_id = orig.id
            WHERE v.id = ?
        """
        async with self.db.transaction() as conn:
            async with conn.execute(sql, (video_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    raise VideoNotFoundError("Video not found")
                return dict(row)

    async def toggle_short(self, video_id: int) -> dict[str, Any]:
        """Переключает статус is_short для видео и запускает его переиндексацию."""
        video = await self.video_repo.get_by_id(video_id)
        if not video:
            raise VideoNotFoundError("Video not found")

        new_is_short = not bool(video["is_short"])
        await self.video_repo.update(video_id, is_short=new_is_short)

        source_file_id = str(video["source_file_id"]) if video["source_file_id"] else ""
        if source_file_id:
            norm_path = self.settings.get_normalized_transcript_path(source_file_id)
            if norm_path.exists():
                # Чтение и разбор транскрипта
                def read_gzip_json(p: Path) -> dict[str, Any]:
                    with gzip.open(p, "rt", encoding="utf-8") as f:
                        return dict(json.load(f))

                loop = asyncio.get_running_loop()
                norm_payload = await loop.run_in_executor(None, read_gzip_json, norm_path)
                raw_chunks = norm_payload.get("utterances") or norm_payload.get("chunks") or []

                # Перегенерация чанков
                new_chunks = chunk_from_utterances(raw_chunks, single_chunk=new_is_short)

                # Удаление старых точек в Manticore
                old_chunks = await self.chunk_repo.get_by_video_id(video_id)
                old_chunk_ids = [c["id"] for c in old_chunks]
                if old_chunk_ids:
                    await self.manticore.delete_points(self.settings.manticore_table, old_chunk_ids)

                # Замена чанков в БД
                await self.chunk_repo.replace_chunks(video_id, new_chunks)

                # Создание задачи переиндексации
                await self.task_repo.create_task(
                    task_type="stage_3_index",
                    payload={"video_id": video_id},
                    priority=5,
                    video_id=video_id,
                )

                # Запуск воркера (через импорт)
                from app.worker import get_worker

                worker = get_worker()
                if not worker.is_running:
                    asyncio.create_task(worker.run())

        return {"status": "ok", "is_short": new_is_short, "queued": True}

    async def delete_video(self, video_id: int) -> None:
        """Удаляет видео, его локальные файлы, чанки и точки в Manticore."""
        video = await self.video_repo.get_by_id(video_id)
        if not video:
            raise VideoNotFoundError("Video not found")

        source_file_id = video["source_file_id"]

        # Проверка активных задач
        sql_check = """
            SELECT id FROM tasks
            WHERE status IN ('pending', 'running')
              AND (video_id = ? OR json_extract(payload, '$.file_id') = ? OR json_extract(payload, '$.video_id') = ?)
        """
        async with self.db.transaction() as conn:
            async with conn.execute(sql_check, (video_id, source_file_id, video_id)) as cursor:
                active_task = await cursor.fetchone()
                if active_task:
                    raise VideoProcessingError("Нельзя удалить видео, которое сейчас обрабатывается воркером.")

        # Получаем чанки для удаления из Manticore
        chunks = await self.chunk_repo.get_by_video_id(video_id)
        chunk_ids = [c["id"] for c in chunks]

        # Удаление из Manticore
        if chunk_ids:
            try:
                await self.manticore.delete_points(self.settings.manticore_table, chunk_ids)
            except Exception as e:
                logger.error(f"Failed to delete Manticore points for video {video_id}: {e}")

        # Удаление локальных файлов и архивация транскрипта
        if source_file_id:
            await self._delete_physical_files(video_id, str(source_file_id))

        # Удаление записи о видео и логов целостности из БД
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))

            # Чистим логи целостности
            async with conn.execute("SELECT id, message FROM integrity_issues") as cursor:
                ii_rows = await cursor.fetchall()
                for row in ii_rows:
                    msg = row["message"]
                    id_match = (
                        re.search(r"\(ID:(\d+)\)", msg)
                        or re.search(r"Video (\d+):", msg)
                        or re.search(r"video ID (\d+):", msg, re.IGNORECASE)
                    )
                    if id_match and int(id_match.group(1)) == video_id:
                        await conn.execute("DELETE FROM integrity_issues WHERE id = ?", (row["id"],))

    async def _delete_physical_files(self, video_id: int, source_file_id: str) -> None:
        archive_dir = self.settings.app_storage_dir / "transcripts" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        files_to_delete: list[Path] = []
        wav_p = self.settings.audio_dir / f"{source_file_id}.wav"
        ogg_p = self.settings.audio_dir / f"{source_file_id}.ogg"
        for p in [wav_p, ogg_p]:
            if p.exists():
                files_to_delete.append(p)

        downloads_dir = self.settings.downloads_dir
        if downloads_dir.exists():
            for p in downloads_dir.glob(f"{source_file_id}*"):
                files_to_delete.append(p)

        raw_path = self.settings.get_raw_transcript_path(source_file_id)
        if raw_path.exists():

            def archive_raw():
                archive_dest = archive_dir / f"video_{video_id}_{source_file_id}.json.gz"
                shutil.move(str(raw_path), str(archive_dest))
                if raw_path.parent.exists() and not any(raw_path.parent.iterdir()):
                    raw_path.parent.rmdir()

            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, archive_raw)
            except Exception as e:
                logger.error(f"Failed to move raw transcript {raw_path}: {e}")
                files_to_delete.append(raw_path)

        norm_path = self.settings.get_normalized_transcript_path(source_file_id)
        if norm_path.exists():
            files_to_delete.append(norm_path)

        def unlink_files(files: list[Path]):
            for f_path in files:
                try:
                    if f_path.exists():
                        f_path.unlink()
                        if f_path.parent.exists() and f_path.parent not in (
                            self.settings.raw_transcripts_dir,
                            self.settings.normalized_transcripts_dir,
                        ):
                            if not any(f_path.parent.iterdir()):
                                f_path.parent.rmdir()
                except Exception as ex:
                    logger.error(f"Failed to delete file {f_path}: {ex}")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, unlink_files, files_to_delete)

    async def reindex_video(self, video_id: int) -> None:
        """Перезапускает переиндексацию видео, создавая новую задачу скачивания."""
        video = await self.video_repo.get_by_id(video_id)
        if not video:
            raise VideoNotFoundError("Video not found")

        source_file_id = str(video["source_file_id"]) if video["source_file_id"] else ""
        title = str(video["title"])

        # Проверка активных задач
        sql_check = """
            SELECT id FROM tasks
            WHERE status IN ('pending', 'running')
              AND (video_id = ? OR json_extract(payload, '$.file_id') = ? OR json_extract(payload, '$.video_id') = ?)
        """
        async with self.db.transaction() as conn:
            async with conn.execute(sql_check, (video_id, source_file_id, video_id)) as cursor:
                active_task = await cursor.fetchone()
                if active_task:
                    raise VideoProcessingError("Видео уже находится в обработке или в очереди.")

        # Постановка задачи
        payload: dict[str, str | int | bool | None] = {
            "file_id": source_file_id,
            "title": title,
            "diarize": True,
            "reindex": True,
            "video_id": video_id,
        }
        await self.task_repo.create_task(
            task_type="stage_1_download",
            payload=payload,
            priority=8,
            video_id=video_id,
        )

        # Сброс статуса видео
        await self.video_repo.update(video_id, status="pending", duration_sec=None, size_bytes=None)

        # Запуск воркера
        from app.worker import get_worker

        worker = get_worker()
        if not worker.is_running:
            asyncio.create_task(worker.run())

    async def mark_video_silent(self, video_id: int) -> None:
        """Помечает видео как тихое, удаляет его чанки из SQLite и Manticore и сбрасывает задачи."""
        video = await self.video_repo.get_by_id(video_id)
        if not video:
            raise VideoNotFoundError("Video not found")

        source_file_id = video["source_file_id"]

        # 1. Получаем чанки
        chunks = await self.chunk_repo.get_by_video_id(video_id)
        chunk_ids = [c["id"] for c in chunks]

        # 2. Удаляем из Manticore
        if chunk_ids:
            try:
                await self.manticore.delete_points(self.settings.manticore_table, chunk_ids)
            except Exception as e:
                logger.error(f"Failed to delete Manticore points for video {video_id}: {e}")

        # 3. Удаляем из SQLite, отменяем задачи и помечаем
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))
            await conn.execute(
                """
                DELETE FROM tasks
                WHERE video_id = ?
                   OR json_extract(payload, '$.file_id') = ?
                   OR json_extract(payload, '$.video_id') = ?
                """,
                (video_id, source_file_id, video_id),
            )
            await conn.execute(
                """
                UPDATE videos
                SET is_silent = 1, status = 'indexed_chunks_ready', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (video_id,),
            )

            # Удаляем лог целостности
            async with conn.execute("SELECT id, message FROM integrity_issues") as cursor:
                ii_rows = await cursor.fetchall()
                for row in ii_rows:
                    msg = row["message"]
                    id_match = (
                        re.search(r"\(ID:(\d+)\)", msg)
                        or re.search(r"Video (\d+):", msg)
                        or re.search(r"video ID (\d+):", msg, re.IGNORECASE)
                    )
                    if id_match and int(id_match.group(1)) == video_id:
                        await conn.execute("DELETE FROM integrity_issues WHERE id = ?", (row["id"],))

    async def update_chunk_text(self, chunk_id: int, new_text: str) -> dict[str, Any]:
        """Обновляет текст чанка в SQLite, векторов в Manticore и gzip файле на диске."""
        # 1. Fetch chunk info for payload
        sql = """
            SELECT
                c.chunk_index, c.video_id, c.start_sec, c.end_sec,
                v.title, v.source_file_id, v.source_url
            FROM chunks c
            JOIN videos v ON v.id = c.video_id
            WHERE c.id = ?
        """
        async with self.db.transaction() as conn:
            async with conn.execute(sql, (chunk_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    raise VideoNotFoundError("Chunk not found")
                row_dict = dict(row)

            # 2. Update SQLite
            await conn.execute("UPDATE chunks SET text = ? WHERE id = ?", (new_text, chunk_id))

        # 3. Generate new vectors
        vector_updated = False
        try:
            dense_vec, sparse_vec = await self.embedder.embed_text(new_text, task_type="RETRIEVAL_DOCUMENT")

            # 4. Update Manticore
            vectors: dict[str, Any] = {"default": dense_vec}
            if sparse_vec:
                vectors["text-sparse"] = sparse_vec

            points = [
                {
                    "id": chunk_id,
                    "vector": vectors,
                    "payload": {
                        "chunk_id": chunk_id,
                        "video_id": str(row_dict["video_id"]),
                        "chunk_index": row_dict["chunk_index"],
                        "start_sec": row_dict["start_sec"],
                        "end_sec": row_dict["end_sec"],
                        "text": new_text,
                        "title": row_dict["title"],
                        "source_file_id": row_dict["source_file_id"],
                        "source_url": row_dict["source_url"],
                        "is_primary": True,
                    },
                }
            ]
            await self.manticore.upsert_points(self.settings.manticore_table, points)
            vector_updated = True
        except Exception as e:
            logger.error(f"Failed to update vectors in Manticore for chunk {chunk_id}: {e}")

        # 5. Update physical file on disk
        source_file_id = row_dict["source_file_id"]
        if source_file_id:
            path = self.settings.get_normalized_transcript_path(source_file_id)
            if path.exists():

                def sync_file():
                    with gzip.open(path, "rt", encoding="utf-8") as f:
                        content = json.load(f)

                    chunk_index = row_dict["chunk_index"]
                    if "utterances" in content and len(content["utterances"]) > chunk_index:
                        content["utterances"][chunk_index]["text"] = new_text
                    elif "chunks" in content and len(content["chunks"]) > chunk_index:
                        content["chunks"][chunk_index]["text"] = new_text

                    if "utterances" in content:
                        content["transcript"] = " ".join(u["text"] for u in content["utterances"])

                    with gzip.open(path, "wt", encoding="utf-8") as f:
                        json.dump(content, f, separators=(",", ":"), ensure_ascii=False)

                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, sync_file)
                except Exception as e:
                    logger.error(f"Failed to sync JSON file {path}: {e}")

        return {"status": "updated", "vector_updated": vector_updated}
