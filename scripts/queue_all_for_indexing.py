import logging
import sys
from pathlib import Path

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_sqlite_settings
from app.db import db_connection
from app.indexing_state import enqueue_index_task

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def queue_all():
    pg_settings = get_sqlite_settings()
    # Queue idempotent updates. Existing search data remains available until replaced.
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
            task_id = enqueue_index_task(conn, video_id=v["id"], title=v["title"], priority=10)
            count += int(task_id is not None)

        logger.info(f"Successfully added {count} indexing tasks to the queue.")


if __name__ == "__main__":
    queue_all()
