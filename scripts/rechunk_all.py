import gzip
import json
import sys
from pathlib import Path

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.chunking import chunk_from_utterances
from app.config import get_app_settings, get_sqlite_settings
from app.db import db_connection
from app.indexing_state import enqueue_index_task
from app.repository import replace_chunks


def main():
    app_settings = get_app_settings()
    pg_settings = get_sqlite_settings()
    with db_connection(pg_settings) as conn:
        # Получаем список видео
        videos = conn.execute(
            "SELECT id, source_file_id, title, is_short FROM videos WHERE original_id IS NULL"
        ).fetchall()

        print(f"Found {len(videos)} original videos in DB.")

        count = 0
        for v in videos:
            v_id = v["id"]
            file_id = v["source_file_id"]
            title = v["title"]
            is_short = bool(v["is_short"])

            if not file_id:
                continue

            # Находим нормализованную транскрипцию
            norm_path = app_settings.get_normalized_transcript_path(file_id)
            if not norm_path.exists():
                # Попробуем найти по правилу без префикса (на всякий случай)
                legacy_path = Path(app_settings.storage_dir) / "transcripts" / "normalized" / f"{file_id}.json.gz"
                if legacy_path.exists():
                    norm_path = legacy_path
                else:
                    print(f"Skipping video {v_id} ({title}): Normalized transcript not found at {norm_path}")
                    continue

            try:
                with gzip.open(norm_path, "rt", encoding="utf-8") as f:
                    norm_payload = json.load(f)
            except Exception as e:
                print(f"Error loading normalized transcript for {file_id}: {e}")
                continue

            raw_chunks = norm_payload.get("utterances") or norm_payload.get("chunks") or []
            if not raw_chunks:
                continue

            # Перенарезаем чанки
            chunks = chunk_from_utterances(raw_chunks, single_chunk=is_short)

            # Сохраняем чанки в SQLite
            replace_chunks(conn, video_id=v_id, chunks=chunks)

            # Создаем задачу на индексацию
            task_id = enqueue_index_task(conn, video_id=v_id, title=title, priority=10)
            count += int(task_id is not None)
            if count % 100 == 0:
                print(f"Rechunked and queued {count} videos...")

        print(f"\nSuccessfully re-chunked and queued {count} videos for re-indexing!")


if __name__ == "__main__":
    main()
