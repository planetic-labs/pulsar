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
from app.repository import replace_chunks, update_video_status, upsert_video
from app.transcription.deepgram import DeepgramEngine
from app.transcription.postprocessing import apply_postprocessing_to_raw


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

        # Ищем все .json.gz файлы в raw/
        for latest_file in raw_dir.glob("**/*.json.gz"):
            file_id = latest_file.name[:-8]
            print(f"Processing video: {file_id}")

            video_id = upsert_video(
                conn,
                source_file_id=file_id,
                title=f"Rebuilt: {file_id}",  # Titles will be synced later
                source_url=f"https://drive.google.com/file/d/{file_id}/view",
                status="transcribed",
                mime_type=None,
                size_bytes=None,
                duration_sec=None,
            )

            print(f"  - File: {latest_file.name}")
            try:
                import gzip

                with gzip.open(latest_file, "rt", encoding="utf-8") as f:
                    raw_payload = json.load(f)
            except Exception as e:
                print(f"    Error loading JSON: {e}")
                continue

            # Apply post-processing (e.g. мастер -> Мастер)
            raw_payload = apply_postprocessing_to_raw(raw_payload)

            try:
                normalized = engine.normalize_response(raw_payload)
            except Exception as e:
                print(f"    Error normalizing: {e}")
                continue

            norm_path = app_settings.get_normalized_transcript_path(file_id)
            norm_path.parent.mkdir(parents=True, exist_ok=True)
            import gzip

            with gzip.open(norm_path, "wt", encoding="utf-8") as f:
                json.dump(normalized, f, separators=(",", ":"), ensure_ascii=False)

            chunks = chunk_from_utterances(normalized.get("utterances", []))
            replace_chunks(conn, video_id=video_id, chunks=chunks)

            update_video_status(conn, video_id=video_id, status="completed")

    print("\nRebuild finished!")


if __name__ == "__main__":
    rebuild()
