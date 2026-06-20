from __future__ import annotations

import gzip
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.chunking import chunk_from_utterances
from app.config import (
    get_app_settings,
    get_deepgram_settings,
    get_sqlite_settings,
)
from app.db import db_connection
from app.pipeline.base import PipelineStage, StageResult
from app.repository import replace_chunks, upsert_video
from app.transcription.deepgram import DeepgramEngine
from app.transcription.postprocessing import apply_postprocessing_to_raw

logger = logging.getLogger("app.pipeline.transcribe")


class TranscribeStage(PipelineStage):
    """Stage 2: Транскрибация аудиофайла через Deepgram, сохранение чанков в БД."""

    def stage_name(self) -> str:
        return "stage_2_transcribe"

    async def execute(
        self,
        task_id: int,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> StageResult:
        file_id = payload["file_id"]
        audio_path = payload["audio_path"]
        title = payload.get("title", file_id)

        # 1. Проверка баланса Deepgram перед стартом
        dg_settings = get_deepgram_settings()
        engine = DeepgramEngine(dg_settings)
        is_ok, amount = await engine.check_balance_threshold_async(1.0)

        if not is_ok:
            err_msg = f"Отказ в транскрибации: баланс Deepgram (${amount:.2f}) ниже порога $1.00"
            logger.error(err_msg)
            return StageResult(success=False, error=err_msg)

        audio_p = Path(audio_path)
        if not audio_p.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        logger.info(f"Транскрибация: {title}")

        if progress_callback:
            progress_callback(
                {"active": True, "title": title, "progress": 0, "speed": "", "status_text": "Инициализация"}
            )

        last_log_time = time.time()
        last_ui_update_time = 0.0
        last_uploaded = 0

        def upload_progress(uploaded: int, total: int) -> None:
            nonlocal last_log_time, last_uploaded, last_ui_update_time
            current_time = time.time()
            is_finished = uploaded == total

            if is_finished or (current_time - last_ui_update_time >= 2):
                percent = int((uploaded / total) * 100) if total else 0
                duration = current_time - (last_ui_update_time or last_log_time)
                delta = uploaded - last_uploaded
                speed_bps = delta / duration if duration > 0 else 0

                if speed_bps >= 1024 * 1024:
                    speed_str = f"{speed_bps / (1024 * 1024):.1f} MB/s"
                else:
                    speed_str = f"{speed_bps / 1024:.1f} KB/s"

                if progress_callback:
                    if percent >= 100:
                        progress_callback(
                            {"status_text": "Файл загружен. Ожидание Deepgram...", "progress": 99, "speed": speed_str}
                        )
                    else:
                        progress_callback({"status_text": "Отправка файла", "progress": percent, "speed": speed_str})

                last_ui_update_time = current_time
                last_uploaded = uploaded

        app_settings = get_app_settings()
        raw_path = app_settings.get_raw_transcript_path(file_id)
        norm_path = app_settings.get_normalized_transcript_path(file_id)

        skip_transcription = False
        if raw_path.exists() and norm_path.exists():
            logger.info(f"Транскрипты для {file_id} уже существуют на диске. Пропускаем запрос к Deepgram.")
            try:
                with gzip.open(norm_path, "rt", encoding="utf-8") as f:
                    norm_payload = json.load(f)
                skip_transcription = True
            except Exception as e:
                logger.warning(
                    f"Не удалось прочитать существующий транскрипт {norm_path}: {e}. Запуск новой транскрибации."
                )

        if not skip_transcription:
            # Отправка и получение транскрипта
            raw_payload = await engine.transcribe_file_async(
                audio_p,
                diarize=payload.get("diarize", True),
                progress_callback=upload_progress,
            )

            # Применение постобработки
            raw_payload = apply_postprocessing_to_raw(raw_payload)

            # Нормализация данных
            norm_payload = engine.normalize_response(raw_payload)

            # Сохранение файлов транскрипта на диск (.json.gz) с temp-then-rename
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            norm_path.parent.mkdir(parents=True, exist_ok=True)

            raw_tmp = raw_path.with_suffix(".tmp")
            norm_tmp = norm_path.with_suffix(".tmp")

            try:
                with gzip.open(raw_tmp, "wt", encoding="utf-8") as f:
                    json.dump(raw_payload, f, separators=(",", ":"), ensure_ascii=False)
                with gzip.open(norm_tmp, "wt", encoding="utf-8") as f:
                    json.dump(norm_payload, f, separators=(",", ":"), ensure_ascii=False)

                raw_tmp.rename(raw_path)
                norm_tmp.rename(norm_path)
            except Exception as e:
                for tmp_file in [raw_tmp, norm_tmp]:
                    try:
                        if tmp_file.exists():
                            tmp_file.unlink()
                    except OSError:
                        pass
                raise e

        if progress_callback:
            progress_callback({"status_text": "Сохранение в базу...", "progress": 99, "speed": ""})

        # Операции с базой данных
        sqlite_settings = get_sqlite_settings()
        with db_connection(sqlite_settings) as conn:
            duration_sec = float(norm_payload.get("duration", 0.0)) or None
            is_short = bool(duration_sec and duration_sec <= 1800)

            video_id = upsert_video(
                conn,
                source_file_id=file_id,
                parent_folder_id=payload.get("parent_folder_id"),
                md5_checksum=payload.get("md5_checksum"),
                title=title,
                source_url=f"https://drive.google.com/file/d/{file_id}/view",
                mime_type=payload.get("mime_type", "video/mp4"),
                size_bytes=None,
                duration_sec=duration_sec,
                is_short=is_short,
                status="transcribed",
            )

            raw_chunks = norm_payload.get("utterances") or norm_payload.get("chunks") or []
            chunks_data = chunk_from_utterances(raw_chunks, single_chunk=is_short)
            replace_chunks(conn, video_id=video_id, chunks=chunks_data)

        # Удаление аудиофайла только при успешном завершении
        if audio_p.exists():
            audio_p.unlink()

        logger.info(f"Текст для {title} сохранен.")
        return StageResult(success=True, next_payload={"video_id": video_id, "title": title})
