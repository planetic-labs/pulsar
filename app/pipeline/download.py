from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from typing import Any

from app.audio import SilentVideoError, convert_wav_to_ogg, extract_audio
from app.config import (
    get_app_settings,
    get_google_drive_settings,
    get_manticore_settings,
    get_sqlite_settings,
)
from app.db import db_connection
from app.google_drive import GoogleDriveClient
from app.manticore import get_manticore_client
from app.pipeline.base import PipelineStage, StageResult
from app.repository import upsert_video

logger = logging.getLogger("app.pipeline.download")


class InsufficientSpaceError(Exception):
    """Исключение, выбрасываемое при нехватке места на диске."""

    def __init__(self, message: str, file_size: int = 0):
        super().__init__(message)
        self.file_size = file_size


class DownloadStage(PipelineStage):
    """Stage 1: Скачивание видео с Google Drive и извлечение аудио."""

    def stage_name(self) -> str:
        return "stage_1_download"

    async def execute(
        self,
        task_id: int,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> StageResult:
        file_id = payload["file_id"]
        title = payload.get("title", f"File {file_id}")

        if progress_callback:
            progress_callback(
                {"active": True, "title": title, "progress": 0, "speed": "", "status_text": "Инициализация"}
            )

        # 1. Очистка данных перед переиндексацией (если требуется)
        if payload.get("reindex") and payload.get("video_id"):
            video_id = payload["video_id"]
            logger.info(f"Очистка данных перед переиндексацией для видео ID {video_id} ({title})")
            await self._cleanup_before_reindex(video_id, file_id, title)

        # Получаем количество задач в очереди для логов/статусов
        sql_q = "SELECT COUNT(*) as c FROM tasks WHERE status IN ('pending', 'running')"
        with db_connection(get_sqlite_settings()) as conn:
            c_row = conn.execute(sql_q).fetchone()
            in_queue = c_row["c"]

        # 2. Инициализация Google Drive клиента и проверка MD5
        drive_settings = get_google_drive_settings()
        app_settings = get_app_settings()
        drive_client = GoogleDriveClient(drive_settings)

        try:
            if progress_callback:
                progress_callback({"status_text": "Проверка MD5"})
            drive_file = await drive_client.get_file(file_id)
            md5 = drive_file.md5_checksum
        except Exception as e:
            logger.error(f"Не удалось получить метаданные Google Drive для файла {file_id}: {e}")
            md5 = None

        if md5:
            # Проверка дубликатов по MD5
            with db_connection(get_sqlite_settings()) as conn:
                existing = conn.execute(
                    "SELECT id, title, source_file_id FROM videos "
                    "WHERE md5_checksum = ? AND original_id IS NULL AND source_file_id != ?",
                    (md5, file_id),
                ).fetchone()

            if existing:
                logger.warning(
                    f"Файл {title} ({file_id}) является дубликатом по MD5 файла "
                    f"{existing['title']} ({existing['source_file_id']})! Скачивание пропущено."
                )

                parent_folder_id = (
                    drive_file.parents[0]
                    if hasattr(drive_file, "parents") and drive_file.parents
                    else payload.get("parent_folder_id")
                )

                with db_connection(get_sqlite_settings()) as conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO videos (
                            source_file_id, parent_folder_id, md5_checksum, title,
                            status, original_id, source_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (source_file_id) DO UPDATE SET
                            parent_folder_id = EXCLUDED.parent_folder_id,
                            md5_checksum = EXCLUDED.md5_checksum,
                            title = EXCLUDED.title,
                            status = EXCLUDED.status,
                            original_id = EXCLUDED.original_id,
                            source_url = EXCLUDED.source_url,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING id
                        """,
                        (
                            file_id,
                            parent_folder_id,
                            md5,
                            title,
                            "skipped_duplicate_md5",
                            existing["id"],
                            f"https://drive.google.com/file/d/{file_id}/view",
                        ),
                    )
                    inserted_vid = cursor.fetchone()["id"]

                return StageResult(
                    success=True,
                    status="skipped_duplicate_md5",
                    next_payload={"video_id": inserted_vid},
                )

        # 3. Проверка свободного места
        file_size = int(drive_file.size or 0) if "drive_file" in locals() else 0
        required_space = (app_settings.disk_space_buffer_gb * 1024**3) + file_size
        total, used, free = shutil.disk_usage(str(app_settings.downloads_dir))
        if free < required_space:
            raise InsufficientSpaceError(
                f"Недостаточно места на диске. Требуется: {required_space / 1024**3:.2f} ГБ, "
                f"Свободно: {free / 1024**3:.2f} ГБ (Буфер: {app_settings.disk_space_buffer_gb} ГБ)",
                file_size=file_size,
            )

        video_path = app_settings.downloads_dir / f"{file_id}.mp4"
        audio_path = app_settings.audio_dir / f"{file_id}.wav"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.parent.mkdir(parents=True, exist_ok=True)

        def format_title(t: str, max_len: int = 40) -> str:
            if len(t) <= max_len:
                return t
            return t[: max_len - 3] + "..."

        clean_title = format_title(drive_file.name if "drive_file" in locals() else title)
        logger.info(f"Загрузка: (старт) {clean_title} (в очереди: {in_queue})")

        if progress_callback:
            progress_callback({"status_text": "Начало загрузки...", "progress": 0, "speed": ""})

        last_log_time = time.time()
        last_ui_update_time = 0.0
        last_downloaded = 0

        def download_progress(downloaded: int, total_bytes: int) -> None:
            nonlocal last_log_time, last_downloaded, last_ui_update_time
            current_time = time.time()
            is_finished = downloaded == total_bytes

            if is_finished or (current_time - last_ui_update_time >= 2):
                percent = int((downloaded / total_bytes) * 100) if total_bytes else 0
                duration = current_time - (last_ui_update_time or last_log_time)
                delta = downloaded - last_downloaded
                speed_bps = delta / duration if duration > 0 else 0

                if speed_bps >= 1024 * 1024:
                    speed_str = f"{speed_bps / (1024 * 1024):.1f} MB/s"
                else:
                    speed_str = f"{speed_bps / 1024:.1f} KB/s"

                if progress_callback:
                    if percent >= 100:
                        progress_callback(
                            {"status_text": "Файл скачан. Извлечение аудио...", "progress": 99, "speed": speed_str}
                        )
                    else:
                        progress_callback({"status_text": "Загрузка файла", "progress": percent, "speed": speed_str})

                last_ui_update_time = current_time
                last_downloaded = downloaded

        ogg_path = None
        try:
            await drive_client.download_file(file_id, video_path, progress_callback=download_progress)
            logger.info(f"Загрузка: (завершена) {clean_title}")

            if progress_callback:
                progress_callback({"status_text": "Извлечение аудио...", "progress": 99, "speed": ""})

            v_size_mb = video_path.stat().st_size / (1024 * 1024)
            logger.info(f"Извлечение аудио: {clean_title} ({v_size_mb:.1f} MB)")

            # Выполняем ресурсоемкое извлечение в отдельном потоке
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: extract_audio(video_path, audio_path))

            wav_size_mb = audio_path.stat().st_size / (1024 * 1024)
            if wav_size_mb > app_settings.max_audio_size_mb:
                ogg_path = audio_path.with_suffix(".ogg")
                logger.info(
                    f"Сжатие: {clean_title} ({wav_size_mb:.1f} MB WAV "
                    f"превышает лимит {app_settings.max_audio_size_mb} MB) -> OGG"
                )
                if progress_callback:
                    progress_callback({"status_text": "Сжатие аудио...", "progress": 99, "speed": ""})

                await loop.run_in_executor(None, lambda: convert_wav_to_ogg(audio_path, ogg_path))

                if audio_path.exists():
                    audio_path.unlink()
                audio_path = ogg_path
        except SilentVideoError:
            logger.warning(f"Видео без звука: {title}. Добавляем в индекс с пометкой (без звука).")
            parent_folder_id = (
                drive_file.parents[0]
                if "drive_file" in locals() and drive_file.parents
                else payload.get("parent_folder_id")
            )
            if not parent_folder_id or not md5:
                try:
                    drive_file = await drive_client.get_file(file_id)
                    if not parent_folder_id and drive_file.parents:
                        parent_folder_id = drive_file.parents[0]
                    if not md5:
                        md5 = drive_file.md5_checksum
                except Exception as e:
                    logger.error(f"Не удалось получить метаданные для видео без звука {file_id}: {e}")

            with db_connection(get_sqlite_settings()) as conn:
                video_id = upsert_video(
                    conn,
                    source_file_id=file_id,
                    parent_folder_id=parent_folder_id,
                    md5_checksum=md5,
                    title=title,
                    source_url=f"https://drive.google.com/file/d/{file_id}/view",
                    mime_type=payload.get("mime_type") or "video/mp4",
                    size_bytes=None,
                    duration_sec=None,
                    is_short=False,
                    status="indexed_chunks_ready",
                )
                conn.execute("UPDATE videos SET is_silent = 1 WHERE id = ?", (video_id,))

            return StageResult(
                success=True,
                status="completed_silent",
                next_payload={"video_id": video_id},
            )
        except Exception:
            if video_path.exists():
                video_path.unlink()
            if audio_path.exists():
                audio_path.unlink()
            if ogg_path and ogg_path.exists():
                ogg_path.unlink()
            raise
        finally:
            if video_path.exists():
                video_path.unlink()

        # Скачивание прошло успешно, возвращаем путь к аудио
        next_payload = {
            **payload,
            "audio_path": str(audio_path),
            "title": drive_file.name if "drive_file" in locals() else title,
            "mime_type": drive_file.mime_type
            if "drive_file" in locals()
            else (payload.get("mime_type") or "video/mp4"),
            "md5_checksum": md5,
            "parent_folder_id": drive_file.parents[0] if ("drive_file" in locals() and drive_file.parents) else None,
        }

        return StageResult(success=True, next_payload=next_payload)

    async def _cleanup_before_reindex(self, video_id: int, file_id: str, title: str) -> None:
        # 1. Удаление векторов из Manticore
        m_settings = get_manticore_settings()
        try:
            manticore = get_manticore_client()
            with db_connection(get_sqlite_settings()) as conn:
                chunk_rows = conn.execute("SELECT id FROM chunks WHERE video_id = ?", (video_id,)).fetchall()
            chunk_ids = [c["id"] for c in chunk_rows]
            if chunk_ids:
                manticore.delete(collection_name=m_settings.table_name, ids=chunk_ids)
                logger.info(f"Удалено {len(chunk_ids)} векторов из Manticore для видео {video_id}")
        except Exception as e:
            logger.error(f"Ошибка при очистке векторов в Manticore для видео {video_id}: {e}")

        # 2. Удаление локальных файлов из хранилища
        app_settings = get_app_settings()
        wav_p = app_settings.audio_dir / f"{file_id}.wav"
        ogg_p = app_settings.audio_dir / f"{file_id}.ogg"
        for p in [wav_p, ogg_p]:
            try:
                if p.exists():
                    p.unlink()
            except Exception as e:
                logger.error(f"Не удалось удалить файл {p}: {e}")

        downloads_dir = app_settings.downloads_dir
        if downloads_dir.exists():
            for p in downloads_dir.glob(f"{file_id}*"):
                try:
                    if p.exists():
                        p.unlink()
                except Exception as e:
                    logger.error(f"Не удалось удалить временный файл {p}: {e}")

        raw_path = app_settings.get_raw_transcript_path(file_id)
        try:
            if raw_path.exists():
                raw_path.unlink()
                if raw_path.parent.exists() and raw_path.parent not in (
                    app_settings.raw_transcripts_dir,
                    app_settings.normalized_transcripts_dir,
                ):
                    if not any(raw_path.parent.iterdir()):
                        raw_path.parent.rmdir()
        except Exception as e:
            logger.error(f"Не удалось удалить сырой транскрипт {raw_path}: {e}")

        norm_path = app_settings.get_normalized_transcript_path(file_id)
        try:
            if norm_path.exists():
                norm_path.unlink()
                if norm_path.parent.exists() and norm_path.parent not in (
                    app_settings.raw_transcripts_dir,
                    app_settings.normalized_transcripts_dir,
                ):
                    if not any(norm_path.parent.iterdir()):
                        norm_path.parent.rmdir()
        except Exception as e:
            logger.error(f"Не удалось удалить нормализованный транскрипт {norm_path}: {e}")

        # 3. Удаление чанков из SQLite
        with db_connection(get_sqlite_settings()) as conn:
            conn.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))
