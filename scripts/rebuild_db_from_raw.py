import json
import os
import sys
import re
from pathlib import Path

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import (
    get_app_settings, 
    get_postgres_settings, 
    get_transcription_settings, 
    get_deepgram_settings
)
from app.db import db_connection, init_db
from app.repository import upsert_video, replace_transcript, replace_chunks, update_video_status
from app.chunking import chunk_from_utterances
# LocalEmbeddingClient удален, так как смысловой поиск отключен
from app.transcription.factory import get_transcription_engine

def get_engine_id_from_filename(filename: str) -> str:
    match = re.match(r"(.+)_(\d+)\.json", filename)
    if not match:
        return "unknown"
    
    parts = match.group(1).split("_")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return parts[0]

def rebuild():
    app_settings = get_app_settings()
    pg_settings = get_postgres_settings()
    
    # Определяем, какой движок сейчас основной в конфиге
    def get_default_engine_id():
        s = get_transcription_settings()
        if s.engine == "deepgram":
            return f"deepgram:{get_deepgram_settings().model}"
        elif s.engine == "local":
            return f"whisper:{s.whisper_model}"
        return s.engine

    primary_engine_id = get_default_engine_id()
    print(f"Primary engine for indexing: {primary_engine_id}")

    raw_dir = app_settings.raw_transcripts_dir
    if not raw_dir.exists():
        print(f"Raw directory not found: {raw_dir}")
        return

    with db_connection(pg_settings) as conn:
        init_db(conn)
        
        print("Clearing database before rebuild...")
        conn.execute("TRUNCATE TABLE videos, transcripts, chunks RESTART IDENTITY CASCADE")
        
        # Идем по папкам (каждая папка - это source_file_id)
        for video_folder in raw_dir.iterdir():
            if not video_folder.is_dir():
                continue
            
            file_id = video_folder.name
            print(f"\nProcessing video: {file_id}")
            
            engine_files = {}
            for f in video_folder.glob("*.json"):
                eid = get_engine_id_from_filename(f.name)
                ts_match = re.search(r"(\d+)\.json", f.name)
                if not ts_match: continue
                timestamp = int(ts_match.group(1))
                if eid not in engine_files or timestamp > engine_files[eid][1]:
                    engine_files[eid] = (f, timestamp)
            
            if not engine_files:
                continue

            # Ищем существующее видео
            cur = conn.execute("SELECT title, local_video_path, local_audio_path FROM videos WHERE source_file_id = %s", (file_id,))
            existing = cur.fetchone()
            
            title = existing["title"] if existing else f"Video {file_id}"
            v_path = existing["local_video_path"] if existing else ""
            a_path = existing["local_audio_path"] if existing else ""

            video_id = upsert_video(
                conn,
                source_type="google_drive",
                source_file_id=file_id,
                title=title,
                source_url=f"https://drive.google.com/file/d/{file_id}/view",
                mime_type="video/mp4",
                size_bytes=None,
                duration_sec=None,
                local_video_path=v_path,
                local_audio_path=a_path,
                processing_status="transcribed"
            )

            for eid, (f_path, _) in engine_files.items():
                print(f"  - Model: {eid}")
                raw_payload = json.loads(f_path.read_text(encoding="utf-8"))
                
                engine = get_transcription_engine() 
                try:
                    normalized = engine.normalize_response(raw_payload)
                except Exception as e:
                    print(f"    Error normalizing {eid}: {e}")
                    continue

                is_primary = (eid == primary_engine_id)
                norm_filename = f"{file_id}_{eid.replace(':', '_')}.json"
                norm_path = app_settings.normalized_transcripts_dir / norm_filename
                norm_path.parent.mkdir(parents=True, exist_ok=True)
                norm_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")

                transcript_id = replace_transcript(
                    conn,
                    video_id=video_id,
                    engine=eid,
                    language="ru",
                    transcript_text=normalized.get("transcript", ""),
                    confidence=normalized.get("confidence", 1.0),
                    raw_json_path=str(f_path),
                    normalized_json_path=str(norm_path),
                    is_primary=is_primary
                )

                chunks = chunk_from_utterances(normalized.get("utterances", []))
                # Эмбеддинги больше не генерируем
                for chunk in chunks:
                    chunk["embedding"] = None

                replace_chunks(conn, video_id=video_id, transcript_id=transcript_id, chunks=chunks)
            
            update_video_status(conn, video_id=video_id, processing_status="completed")

    print("\nRebuild finished!")

if __name__ == "__main__":
    rebuild()
