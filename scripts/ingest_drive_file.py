from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
import sys
from urllib.parse import quote

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.audio import extract_audio
from app.chunking import chunk_from_utterances
from app.config import (
    get_app_settings, 
    get_google_drive_settings, 
    get_postgres_settings,
    get_transcription_settings
)
from app.db import db_connection, init_db
from app.google_drive import GoogleDriveClient
from app.repository import (
    get_video_by_source_file_id,
    replace_chunks,
    replace_transcript,
    update_video_status,
    upsert_video,
)
from app.search import LocalEmbeddingClient
from app.transcription.factory import get_transcription_engine

def ingest_drive_file(
    file_id: str,
    *,
    title: str | None = None,
    clip_duration_sec: float | None = None,
    clip_start_sec: float = 0.0,
    download_progress_callback: callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    drive_settings = get_google_drive_settings()
    app_settings = get_app_settings()
    pg_settings = get_postgres_settings()
    transcription_settings = get_transcription_settings()

    drive = GoogleDriveClient(drive_settings)
    file_meta = drive.get_file(file_id)

    with db_connection(pg_settings) as connection:
        init_db(connection)
        existing = get_video_by_source_file_id(
            connection,
            source_type="google_drive",
            source_file_id=file_id,
        )
    
    # Create a unique ID for this specific engine+model combination
    # We need it early now to check if THIS specific transcript exists
    current_engine = transcription_settings.engine
    if current_engine == "deepgram":
        from app.config import get_deepgram_settings
        dg_model = get_deepgram_settings().model
        engine_id = f"deepgram:{dg_model}"
    elif current_engine == "local":
        engine_id = f"whisper:{transcription_settings.whisper_model}"
    else:
        engine_id = current_engine

    if existing and existing.get("processing_status") == "indexed_chunks_ready":
        # Check if we already have a transcript for THIS specific engine+model
        from app.repository import check_transcript_exists
        with db_connection(pg_settings) as connection:
            if check_transcript_exists(connection, existing["id"], engine_id):
                return {"video_id": int(existing["id"]), "status": "already_indexed"}

    # Paths
    safe_name = quote(file_meta.name, safe="._-() ").replace("%20", "_")
    video_path = drive_settings.download_dir / safe_name
    audio_path = ROOT_DIR / "audio" / f"{Path(safe_name).stem}.wav"
    
    # 1. Download
    if not video_path.exists():
        drive.download_file(file_id, video_path, progress_callback=download_progress_callback)
    
    # 2. Extract Audio
    if not audio_path.exists():
        extract_audio(video_path, audio_path)

    # 3. Transcribe
    engine = get_transcription_engine()
    
    # Storage paths for "Eternal" transcripts
    raw_filename = f"{engine_id.replace(':', '_')}_{int(time.time())}.json"
    raw_path = app_settings.raw_transcripts_dir / file_id / raw_filename
    norm_filename = f"{file_id}_{engine_id.replace(':', '_')}.json"
    norm_path = app_settings.normalized_transcripts_dir / norm_filename
    
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    norm_path.parent.mkdir(parents=True, exist_ok=True)

    if norm_path.exists():
        normalized_payload = json.loads(norm_path.read_text(encoding="utf-8"))
    else:
        raw_payload = engine.transcribe_file(audio_path)
        normalized_payload = engine.normalize_response(raw_payload)
        
        raw_path.write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        norm_path.write_text(json.dumps(normalized_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4. Save to DB and Generate Embeddings
    embed_client = LocalEmbeddingClient()
    chunks = chunk_from_utterances(normalized_payload.get("utterances", []))
    
    # Determine if this engine should be primary for indexing (matches app config)
    def get_default_engine_id():
        s = get_transcription_settings()
        if s.engine == "deepgram":
            from app.config import get_deepgram_settings
            return f"deepgram:{get_deepgram_settings().model}"
        elif s.engine == "local":
            return f"whisper:{s.whisper_model}"
        return s.engine

    is_primary = (engine_id == get_default_engine_id())

    # Batch generate embeddings ONLY for primary transcript
    for chunk in chunks:
        if is_primary:
            chunk["embedding"] = embed_client.embed_text(chunk["text"], is_query=False)
        else:
            chunk["embedding"] = None

    with db_connection(pg_settings) as connection:
        video_id = upsert_video(
            connection,
            source_type="google_drive",
            source_file_id=file_id,
            title=title or file_meta.name,
            source_url=f"https://drive.google.com/file/d/{file_id}/view",
            mime_type=file_meta.mime_type,
            size_bytes=int(file_meta.size) if file_meta.size else None,
            duration_sec=None,
            local_video_path=str(video_path),
            local_audio_path=str(audio_path),
            processing_status="transcribed",
        )
        
        transcript_id = replace_transcript(
            connection,
            video_id=video_id,
            engine=engine_id,
            language="ru", # Should come from engine
            transcript_text=normalized_payload.get("transcript", ""),
            confidence=normalized_payload.get("confidence", 1.0),
            raw_json_path=raw_path,
            normalized_json_path=norm_path,
            is_primary=is_primary,
        )
        
        replace_chunks(
            connection,
            video_id=video_id,
            transcript_id=transcript_id,
            chunks=chunks,
        )
        
        update_video_status(
            connection,
            video_id=video_id,
            processing_status="indexed_chunks_ready",
        )

    return {"video_id": video_id, "chunks_count": len(chunks)}
