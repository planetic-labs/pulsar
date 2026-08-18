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

            def archive_raw() -> None:
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

        def unlink_files(files: list[Path]) -> None:
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

    async def edit_chunk_text(self, video_id: int, chunk_id: int, new_text: str) -> None:
        """Редактирует текст конкретного чанка, обновляя SQLite, Manticore, Raw/Normalized JSON."""
        # 1. Получаем чанк и проверяем существование
        chunk = await self.chunk_repo.get_chunk_by_id(chunk_id)
        if not chunk or chunk["video_id"] != video_id:
            raise VideoNotFoundError("Chunk not found or mismatch with video")

        video = await self.video_repo.get_by_id(video_id)
        if not video:
            raise VideoNotFoundError("Video not found")

        # Если это дубликат, перенаправляем на оригинал
        if video["original_id"] is not None:
            orig_video_id = int(video["original_id"])
            video_id = orig_video_id
            video = await self.video_repo.get_by_id(orig_video_id)
            if not video:
                raise VideoNotFoundError("Original video not found")

        source_file_id = str(video["source_file_id"])
        start_sec = float(chunk["start_sec"])
        end_sec = float(chunk["end_sec"])
        clean_new_text = new_text.strip()

        # 2. Обновляем JSON-файлы на диске
        raw_path = self.settings.get_raw_transcript_path(source_file_id)
        norm_path = self.settings.get_normalized_transcript_path(source_file_id)

        loop = asyncio.get_running_loop()

        def update_files_on_disk() -> None:
            # 2a. Обновляем Normalized JSON
            if norm_path.exists():
                with gzip.open(norm_path, "rt", encoding="utf-8") as f:
                    norm_data = json.load(f)

                utterances = norm_data.get("utterances", [])
                chunk_utts = [
                    u for u in utterances if float(u.get("start", 0)) >= start_sec and float(u.get("end", 0)) <= end_sec
                ]

                if chunk_utts:
                    self._distribute_words_to_segments(chunk_utts, clean_new_text, text_key="text")
                    norm_tmp = norm_path.with_suffix(".tmp")
                    with gzip.open(norm_tmp, "wt", encoding="utf-8") as f:
                        json.dump(norm_data, f, separators=(",", ":"), ensure_ascii=False)
                    norm_tmp.rename(norm_path)

            # 2b. Обновляем Raw JSON
            if raw_path.exists():
                with gzip.open(raw_path, "rt", encoding="utf-8") as f:
                    raw_data = json.load(f)

                results = raw_data.get("results", {})
                raw_utts = results.get("utterances", [])
                chunk_raw_utts = [
                    u for u in raw_utts if float(u.get("start", 0)) >= start_sec and float(u.get("end", 0)) <= end_sec
                ]
                if chunk_raw_utts:
                    self._distribute_words_to_segments(chunk_raw_utts, clean_new_text, text_key="transcript")

                # Также правим paragraphs -> sentences
                channels = results.get("channels", [])
                if channels:
                    alt = channels[0].get("alternatives", [{}])[0]
                    paragraphs = alt.get("paragraphs", {}).get("paragraphs", [])
                    raw_sentences = []
                    for p in paragraphs:
                        for s in p.get("sentences", []):
                            if float(s.get("start", 0)) >= start_sec and float(s.get("end", 0)) <= end_sec:
                                raw_sentences.append(s)
                    if raw_sentences:
                        self._distribute_words_to_segments(raw_sentences, clean_new_text, text_key="text")

                    # Также правим words list
                    words_list = alt.get("words", [])
                    word_indices = [
                        idx
                        for idx, w in enumerate(words_list)
                        if float(w.get("start", 0)) >= start_sec and float(w.get("end", 0)) <= end_sec
                    ]
                    if word_indices:
                        new_words = clean_new_text.split()
                        start_idx = word_indices[0]
                        end_idx = word_indices[-1]

                        import difflib

                        old_chunk_words = words_list[start_idx : end_idx + 1]
                        old_word_strs = [w.get("word", "") for w in old_chunk_words]

                        matcher = difflib.SequenceMatcher(None, old_word_strs, new_words)
                        aligned_words = []

                        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                            if tag == "equal":
                                for idx in range(i1, i2):
                                    old_w = old_chunk_words[idx]
                                    new_w = new_words[j1 + (idx - i1)]
                                    aligned_words.append(
                                        {
                                            "word": new_w,
                                            "start": old_w.get("start"),
                                            "end": old_w.get("end"),
                                            "confidence": old_w.get("confidence", 1.0),
                                        }
                                    )
                            elif tag == "replace":
                                start_t = old_chunk_words[i1].get("start", start_sec)
                                end_t = old_chunk_words[i2 - 1].get("end", end_sec)
                                duration = end_t - start_t
                                sub_words = new_words[j1:j2]
                                step = duration / max(1, len(sub_words))
                                for k, w in enumerate(sub_words):
                                    w_start = start_t + k * step
                                    w_end = w_start + step
                                    aligned_words.append(
                                        {
                                            "word": w,
                                            "start": w_start,
                                            "end": w_end,
                                            "confidence": 1.0,
                                        }
                                    )
                            elif tag == "delete":
                                pass
                            elif tag == "insert":
                                if i1 > 0:
                                    start_t = old_chunk_words[i1 - 1].get("end", start_sec)
                                else:
                                    start_t = old_chunk_words[0].get("start", start_sec)

                                if i2 < len(old_chunk_words):
                                    end_t = old_chunk_words[i2].get("start", end_sec)
                                else:
                                    end_t = old_chunk_words[-1].get("end", end_sec)

                                duration = end_t - start_t
                                sub_words = new_words[j1:j2]
                                step = duration / max(1, len(sub_words))
                                for k, w in enumerate(sub_words):
                                    w_start = start_t + k * step
                                    w_end = w_start + step
                                    aligned_words.append(
                                        {
                                            "word": w,
                                            "start": w_start,
                                            "end": w_end,
                                            "confidence": 1.0,
                                        }
                                    )

                        words_list[start_idx : end_idx + 1] = aligned_words

                raw_tmp = raw_path.with_suffix(".tmp")
                with gzip.open(raw_tmp, "wt", encoding="utf-8") as f:
                    json.dump(raw_data, f, separators=(",", ":"), ensure_ascii=False)
                raw_tmp.rename(raw_path)

        await loop.run_in_executor(None, update_files_on_disk)

        # 3. Обновляем в SQLite
        await self.chunk_repo.update_chunk(chunk_id, clean_new_text)

        # 4. Рассчитываем эмбеддинг для нового текста
        dense_vec, sparse_vec = await self.embedder.embed_text(clean_new_text, task_type="RETRIEVAL_DOCUMENT")

        # 5. Обновляем в Manticore
        from app.manticore import date_to_int

        vector_data: dict[str, Any] = {"default": dense_vec}
        if sparse_vec:
            vector_data["text-sparse"] = sparse_vec

        m_point = {
            "id": chunk_id,
            "vector": vector_data,
            "payload": {
                "chunk_id": chunk_id,
                "chunk_index": chunk["chunk_index"],
                "text": clean_new_text,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "video_id": str(video_id),
                "title": video["title"],
                "recorded_date": date_to_int(
                    str(video["recorded_date"]) if video["recorded_date"] is not None else None
                ),
                "is_short": bool(video["is_short"]),
                "is_4k": bool(video["is_4k"]),
                "source_file_id": source_file_id,
                "source_url": video["source_url"],
                "is_primary": True,
            },
        }

        await self.manticore.upsert_points(self.settings.manticore_table, [m_point])

        # 6. Очищаем кэш поисковых запросов
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM query_cache")

        # 7. Запускаем мгновенную проверку целостности (Sanity Check)
        await self._verify_chunk_integrity(video_id, chunk_id, clean_new_text, norm_path)

    def _distribute_words_to_segments(self, segments: list[dict], new_text: str, text_key: str = "text") -> None:
        if not segments:
            return
        words = new_text.split()
        if len(segments) == 1:
            segments[0][text_key] = new_text
            return

        lens = [len(str(s.get(text_key, "")).split()) for s in segments]
        total_len = sum(lens)
        if total_len == 0:
            segments[0][text_key] = new_text
        else:
            curr_idx = 0
            for idx, s in enumerate(segments):
                if idx == len(segments) - 1:
                    part_words = words[curr_idx:]
                else:
                    share = int(round(lens[idx] / total_len * len(words)))
                    share = max(1, share)
                    part_words = words[curr_idx : curr_idx + share]
                    curr_idx += share
                s[text_key] = " ".join(part_words)

    async def _verify_chunk_integrity(self, video_id: int, chunk_id: int, expected_text: str, norm_path: Path) -> None:
        """Проверяет успешность записи во все 3 источника: SQLite, JSON, Manticore."""
        db_chunk = await self.chunk_repo.get_chunk_by_id(chunk_id)
        if not db_chunk or db_chunk["text"] != expected_text:
            raise VideoProcessingError("Sanity Check Failed: SQLite chunk text was not updated correctly")

        if norm_path.exists():

            def read_json() -> dict[str, Any]:
                with gzip.open(norm_path, "rt", encoding="utf-8") as f:
                    return dict(json.load(f))

            loop = asyncio.get_running_loop()
            try:
                norm_data = await loop.run_in_executor(None, read_json)
                utterances = norm_data.get("utterances", [])
                chunk_utts = [
                    u
                    for u in utterances
                    if float(u.get("start", 0)) >= db_chunk["start_sec"]
                    and float(u.get("end", 0)) <= db_chunk["end_sec"]
                ]
                if chunk_utts:
                    json_text = " ".join(str(u.get("text", "")).strip() for u in chunk_utts)
                    if json_text != expected_text:
                        raise VideoProcessingError(
                            f"Sanity Check Failed: JSON text mismatch. Expected: {expected_text}, Got: {json_text}"
                        )
            except Exception as e:
                if isinstance(e, VideoProcessingError):
                    raise
                raise VideoProcessingError(f"Sanity Check Failed: JSON validation failed: {e}") from e

        try:
            m_records = await self.manticore.filter_only(
                table=self.settings.manticore_table,
                where_clause=f"chunk_id = {chunk_id}",
                limit=1,
            )
            if not m_records:
                raise VideoProcessingError("Sanity Check Failed: Chunk was not found in Manticore after update")

            m_text = m_records[0].get("payload", {}).get("text")
            if m_text != expected_text:
                raise VideoProcessingError(
                    f"Sanity Check Failed: Manticore text mismatch. Expected: {expected_text}, Got: {m_text}"
                )
        except Exception as e:
            if not isinstance(e, VideoProcessingError):
                raise VideoProcessingError(f"Sanity Check Failed: Manticore verification failed: {e}") from e
            raise
