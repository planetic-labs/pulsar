import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from app.audio import extract_audio
from app.chunking import chunk_from_utterances
from app.config import (
    get_app_settings,
    get_deepgram_settings,
    get_google_drive_settings,
    get_sqlite_settings,
)
from app.db import db_connection
from app.google_drive import GoogleDriveClient
from app.repository import (
    replace_chunks,
    replace_transcript,
    upsert_folder,
    upsert_video,
)
from app.transcription.deepgram import DeepgramEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def download_and_extract_stage(
    file_id: str, status_callback: Callable[[str], None] | None = None, in_queue: int = 0
) -> dict[str, Any]:
    """Stage 1: Download from Drive, Extract Audio, Delete Video."""
    drive_settings = get_google_drive_settings()
    app_settings = get_app_settings()
    drive = GoogleDriveClient(drive_settings)

    file_meta = drive.get_file(file_id)
    video_path = app_settings.storage_dir / "downloads" / f"{file_id}.mp4"
    audio_path = app_settings.storage_dir / "audio" / f"{file_id}.wav"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.parent.mkdir(parents=True, exist_ok=True)

    def format_title(title, max_len=40):
        if len(title) <= max_len:
            return title
        return title[: max_len - 3] + "..."

    clean_title = format_title(file_meta.name)

    if status_callback:
        status_callback(f"Загрузка: (старт) {clean_title} (в очереди: {in_queue})")

    last_log_time = time.time()
    last_downloaded = 0

    def progress_callback(downloaded, total):
        nonlocal last_log_time, last_downloaded
        current_time = time.time()
        duration = current_time - last_log_time
        if duration >= 10:
            percent = int((downloaded / total) * 100) if total else 0

            # Calculate speed
            delta = downloaded - last_downloaded
            speed_bps = delta / duration if duration > 0 else 0

            # Format speed
            if speed_bps >= 1024 * 1024:
                speed_str = f"{speed_bps / (1024 * 1024):.1f} MB/s"
            else:
                speed_str = f"{speed_bps / 1024:.1f} KB/s"

            if status_callback:
                status_callback(f"Загрузка: [{speed_str}] ({percent}%) {clean_title}")

            last_log_time = current_time
            last_downloaded = downloaded

    drive.download_file(file_id, video_path, progress_callback=progress_callback)

    if status_callback:
        status_callback(f"Загрузка: (завершена) {clean_title}")

    if status_callback:
        status_callback(f"Извлечение аудио: {clean_title}")
    extract_audio(video_path, audio_path)

    # Delete video immediately
    if video_path.exists():
        video_path.unlink()

    return {
        "audio_path": str(audio_path),
        "title": file_meta.name,
        "mime_type": file_meta.mime_type,
        "parent_folder_id": file_meta.parents[0] if file_meta.parents else None,
    }


def transcribe_stage(
    file_id: str, audio_path: str, video_metadata: dict, status_callback: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Stage 2: Transcribe via Deepgram, save text to DB, Delete Audio."""
    dg_settings = get_deepgram_settings()
    app_settings = get_app_settings()
    engine = DeepgramEngine(dg_settings)
    pg_settings = get_sqlite_settings()

    audio_p = Path(audio_path)
    if not audio_p.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if status_callback:
        status_callback(f"Транскрибация: {video_metadata['title']}")

    raw_payload = engine.transcribe_file(audio_p, diarize=True)
    norm_payload = engine.normalize_response(raw_payload)

    # Save files
    raw_filename = f"dg_{int(time.time())}.json"
    raw_path = app_settings.raw_transcripts_dir / file_id / raw_filename
    norm_filename = f"{file_id}_dg.json"
    norm_path = app_settings.normalized_transcripts_dir / norm_filename
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    norm_path.parent.mkdir(parents=True, exist_ok=True)

    raw_path.write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    norm_path.write_text(json.dumps(norm_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # DB Operations
    with db_connection(pg_settings) as conn:
        video_id = upsert_video(
            conn,
            source_type="google_drive",
            source_file_id=file_id,
            parent_folder_id=video_metadata.get("parent_folder_id"),
            title=video_metadata["title"],
            source_url=f"https://drive.google.com/file/d/{file_id}/view",
            mime_type=video_metadata["mime_type"],
            size_bytes=None,
            duration_sec=float(norm_payload.get("duration", 0.0)) or None,
            local_video_path=None,
            local_audio_path=str(audio_p),
            processing_status="transcribed",
        )

        transcript_id = replace_transcript(
            conn,
            video_id=video_id,
            language="ru",
            confidence=norm_payload.get("confidence"),
            raw_json_path=raw_path,
            normalized_json_path=norm_path,
        )

        raw_chunks = norm_payload.get("utterances") or norm_payload.get("chunks") or []
        chunks_data = chunk_from_utterances(raw_chunks)
        replace_chunks(conn, video_id=video_id, transcript_id=transcript_id, chunks=chunks_data)

    # Delete audio immediately
    if audio_p.exists():
        audio_p.unlink()

    return {"video_id": video_id}


def ingest_drive_file(file_id: str, diarize: bool = True):
    """Convenience wrapper for legacy scripts (interactive_ingest etc).
    Now it just creates a task in the DB for the worker to pick up.
    """
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        conn.execute(
            "INSERT INTO tasks (task_type, payload) VALUES (?, ?)",
            ("stage_1_download", json.dumps({"file_id": file_id, "diarize": diarize})),
        )
    print(f"Task queued for file {file_id}")


def upsert_folder_chain(drive: GoogleDriveClient, connection: Any, folder_id: str):
    """Recursively upsert folder chain to DB."""
    try:
        f_meta = drive.get_file(folder_id)
        parent_id = f_meta.parents[0] if f_meta.parents else None
        upsert_folder(connection, folder_id=folder_id, name=f_meta.name, parent_id=parent_id)
        if parent_id and parent_id != "root":
            exists = connection.execute("SELECT id FROM folders WHERE id = ?", (parent_id,)).fetchone()
            if not exists:
                upsert_folder_chain(drive, connection, parent_id)
    except Exception as e:
        logger.warning(f"Error in upsert_folder_chain: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file_id")
    parser.add_argument("--diarize", action="store_true")
    args = parser.parse_args()
    ingest_drive_file(args.file_id, diarize=args.diarize)
