import json
import sys
from pathlib import Path

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.chunking import chunk_from_utterances
from app.config import get_app_settings, get_deepgram_settings, get_sqlite_settings
from app.db import db_connection, init_db
from app.repository import replace_chunks, replace_transcript, update_video_status, upsert_video
from app.transcription.deepgram import DeepgramEngine


def rebuild():
    app_settings = get_app_settings()
    pg_settings = get_sqlite_settings()
    dg_settings = get_deepgram_settings()
    engine = DeepgramEngine(dg_settings)

    raw_dir = app_settings.raw_transcripts_dir
    if not raw_dir.exists():
        print(f"Raw directory {raw_dir} not found.")
        return

    with db_connection(pg_settings) as conn:
        init_db(conn)

        # Перебираем папки видео в raw/
        for video_dir in raw_dir.iterdir():
            if not video_dir.is_dir():
                continue

            file_id = video_dir.name
            print(f"Processing video: {file_id}")

            # Находим JSON файлы
            files = list(video_dir.glob("*.json"))
            if not files:
                continue

            # Берем самый свежий файл
            latest_file = max(files, key=lambda x: x.stat().st_mtime)

            video_id = upsert_video(
                conn,
                source_type="google_drive",
                source_file_id=file_id,
                title=f"Rebuilt: {file_id}",  # Titles will be synced later
                source_url=f"https://drive.google.com/file/d/{file_id}/view",
                processing_status="transcribed",
                mime_type=None,
                size_bytes=None,
                duration_sec=None,
                local_video_path=None,
                local_audio_path=None,
            )

            print(f"  - File: {latest_file.name}")
            raw_payload = json.loads(latest_file.read_text(encoding="utf-8"))

            try:
                normalized = engine.normalize_response(raw_payload)
            except Exception as e:
                print(f"    Error normalizing: {e}")
                continue

            norm_filename = f"{file_id}_deepgram.json"
            norm_path = app_settings.normalized_transcripts_dir / norm_filename
            norm_path.parent.mkdir(parents=True, exist_ok=True)
            norm_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")

            transcript_id = replace_transcript(
                conn,
                video_id=video_id,
                language="ru",
                confidence=float(normalized.get("confidence", 1.0)),
                raw_json_path=Path(latest_file),
                normalized_json_path=Path(norm_path),
            )

            chunks = chunk_from_utterances(normalized.get("utterances", []))
            replace_chunks(conn, video_id=video_id, transcript_id=transcript_id, chunks=chunks)

            update_video_status(conn, video_id=video_id, processing_status="completed")

    print("\nRebuild finished!")


if __name__ == "__main__":
    rebuild()
