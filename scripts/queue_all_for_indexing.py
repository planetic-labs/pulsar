import json
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_qdrant_settings, get_sqlite_settings
from app.db import db_connection
from app.qdrant import get_qdrant_client, init_qdrant

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def queue_all():
    pg_settings = get_sqlite_settings()
    q_settings = get_qdrant_settings()

    # 1. Clear Qdrant
    logger.info(f"Clearing Qdrant collection: {q_settings.collection_name}")
    qdrant = get_qdrant_client()
    try:
        qdrant.delete_collection(q_settings.collection_name)
    except Exception as e:
        logger.warning(f"Could not delete collection: {e}")
    init_qdrant()

    # 2. Add tasks to SQLite
    with db_connection(pg_settings) as conn:
        # Получаем все видео, у которых есть чанки
        videos = conn.execute("""
            SELECT DISTINCT v.id, v.title
            FROM videos v
            JOIN chunks c ON c.video_id = v.id
        """).fetchall()

        logger.info(f"Found {len(videos)} videos to index.")

        count = 0
        for v in videos:
            payload = {"video_id": v["id"], "title": v["title"]}
            conn.execute(
                """
                INSERT INTO tasks (task_type, payload, status, priority)
                VALUES (?, ?, ?, ?)
            """,
                ("stage_3_index", json.dumps(payload, ensure_ascii=False), "pending", 10),
            )
            count += 1

        logger.info(f"Successfully added {count} indexing tasks to the queue.")


if __name__ == "__main__":
    queue_all()
