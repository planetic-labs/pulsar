from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from app.config import get_sqlite_settings
from app.db import db_connection
from app.pipeline.download import DownloadStage
from app.pipeline.transcribe import TranscribeStage


async def download_and_extract_stage(
    file_id: str,
    status_callback: Callable[[str], None] | None = None,
    in_queue: int = 0,
    state_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Совместимая обертка над DownloadStage для устаревших скриптов."""
    stage = DownloadStage()
    payload = {"file_id": file_id, "in_queue": in_queue}

    def progress_wrapper(data: dict[str, Any]) -> None:
        if state_callback:
            state_callback(data)
        if status_callback and "status_text" in data:
            status_callback(data["status_text"])

    result = await stage.execute(0, payload, progress_callback=progress_wrapper)
    if not result.success:
        if result.status == "skipped_duplicate_md5":
            # Возвращаем структуру аналогичную успеху для дубликата
            return {
                "audio_path": "",
                "title": payload.get("title", f"File {file_id}"),
                "mime_type": "video/mp4",
                "md5_checksum": "",
                "parent_folder_id": None,
                "video_id": result.next_payload.get("video_id") if result.next_payload else None,
                "skipped_duplicate_md5": True,
            }
        raise RuntimeError(result.error or "Ошибка при скачивании файла")

    assert result.next_payload is not None
    return result.next_payload


async def transcribe_stage(
    file_id: str,
    audio_path: str,
    video_metadata: dict[str, Any],
    status_callback: Callable[[str], None] | None = None,
    state_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Совместимая обертка над TranscribeStage для устаревших скриптов."""
    stage = TranscribeStage()
    payload = {
        "file_id": file_id,
        "audio_path": audio_path,
        **video_metadata,
    }

    def progress_wrapper(data: dict[str, Any]) -> None:
        if state_callback:
            state_callback(data)
        if status_callback and "status_text" in data:
            status_callback(data["status_text"])

    result = await stage.execute(0, payload, progress_callback=progress_wrapper)
    if not result.success:
        raise RuntimeError(result.error or "Ошибка при транскрибации файла")

    assert result.next_payload is not None
    return result.next_payload


def ingest_drive_file(file_id: str, diarize: bool = True) -> None:
    """Создает задачу в БД для обработки видео воркером."""
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        conn.execute(
            "INSERT INTO tasks (task_type, payload) VALUES (?, ?)",
            ("stage_1_download", json.dumps({"file_id": file_id, "diarize": diarize}, ensure_ascii=False)),
        )
    print(f"Task queued for file {file_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file_id")
    parser.add_argument("--diarize", action="store_true")
    args = parser.parse_args()
    ingest_drive_file(args.file_id, diarize=args.diarize)
