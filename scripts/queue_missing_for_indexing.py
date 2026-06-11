import json
import logging
import sys
from pathlib import Path

# Add project root to python path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.qdrant import get_qdrant_client

from app.config import get_qdrant_settings, get_sqlite_settings
from app.db import db_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def queue_missing():
    sqlite_settings = get_sqlite_settings()
    q_settings = get_qdrant_settings()

    qdrant = get_qdrant_client()

    logger.info("Fetching chunk IDs from SQLite...")
    with db_connection(sqlite_settings) as conn:
        sql_chunks = conn.execute("SELECT id FROM chunks").fetchall()
        db_chunk_ids = {r["id"] for r in sql_chunks}

    logger.info(f"Total chunks in SQLite: {len(db_chunk_ids)}")

    logger.info("Fetching point IDs from Qdrant...")
    q_point_ids = set()
    offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=q_settings.collection_name,
            limit=10000,
            with_payload=False,
            with_vectors=False,
            offset=offset,
        )
        for p in points:
            q_point_ids.add(p.id)
        if not next_offset:
            break
        offset = next_offset

    logger.info(f"Total points in Qdrant: {len(q_point_ids)}")

    missing_in_qdrant = db_chunk_ids - q_point_ids
    logger.info(f"Missing chunks in Qdrant: {len(missing_in_qdrant)}")

    if not missing_in_qdrant:
        logger.info("No missing chunks. Everything is in sync!")
        return

    # Find videos for these missing chunks
    missing_list = list(missing_in_qdrant)
    batch_size = 500
    video_map = {}  # video_id -> title

    logger.info("Resolving videos for missing chunks...")
    with db_connection(sqlite_settings) as conn:
        for i in range(0, len(missing_list), batch_size):
            batch = missing_list[i : i + batch_size]
            placeholders = ",".join(["?"] * len(batch))
            sql = f"""
                SELECT DISTINCT v.id, v.title
                FROM chunks c
                JOIN videos v ON v.id = c.video_id
                WHERE c.id IN ({placeholders})
            """
            rows = conn.execute(sql, batch).fetchall()
            for r in rows:
                video_map[r["id"]] = r["title"]

    logger.info(f"Found {len(video_map)} videos requiring reindexing.")

    # Queue indexing tasks
    count = 0
    with db_connection(sqlite_settings) as conn:
        active_tasks = conn.execute(
            "SELECT video_id FROM tasks "
            "WHERE task_type = 'stage_3_index' AND status IN ('pending', 'running') AND video_id IS NOT NULL"
        ).fetchall()
        active_video_ids = {t["video_id"] for t in active_tasks}

        for video_id, title in video_map.items():
            if video_id in active_video_ids:
                logger.info(f"Video '{title}' (ID:{video_id}) is already queued or indexing. Skipping.")
                continue

            payload = {"video_id": video_id, "title": title}
            conn.execute(
                """
                INSERT INTO tasks (video_id, task_type, payload, status, priority)
                VALUES (?, ?, ?, ?, ?)
            """,
                (video_id, "stage_3_index", json.dumps(payload, ensure_ascii=False), "pending", 5),
            )
            logger.info(f"Queued indexing task for video '{title}' (ID:{video_id})")
            count += 1

    logger.info(f"Successfully queued {count} indexing tasks.")


if __name__ == "__main__":
    queue_missing()
