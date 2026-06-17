import asyncio
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_embedding_settings, get_manticore_settings, get_sqlite_settings
from app.embeddings import UnifiedEmbeddingClient
from app.manticore import date_to_int, get_manticore_client, init_manticore
from app.search import hybrid_search

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def main():
    pg_settings = get_sqlite_settings()
    m_settings = get_manticore_settings()

    # 1. Initialize Manticore Table
    logger.info("Initializing Manticore table...")
    init_manticore()
    manticore = get_manticore_client()

    # 2. Get the first 10 videos
    with sqlite3.connect(pg_settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        videos = conn.execute("SELECT id, title FROM videos LIMIT 10").fetchall()
        video_ids = [v["id"] for v in videos]
        video_titles = {v["id"]: v["title"] for v in videos}

        logger.info(f"Selected 10 videos for indexing: {list(video_titles.values())}")

        # Fetch chunks for these videos
        placeholders = ",".join(["?"] * len(video_ids))
        sql = f"""
            SELECT
                c.id as chunk_id, c.video_id, c.chunk_index,
                c.start_sec, c.end_sec, c.text,
                v.title, v.source_file_id, v.source_url, v.is_short, v.is_4k, v.recorded_date
            FROM chunks c
            JOIN videos v ON v.id = c.video_id
            WHERE c.video_id IN ({placeholders})
            ORDER BY c.id ASC
            LIMIT 100
        """
        rows = conn.execute(sql, video_ids).fetchall()

    if not rows:
        logger.error("No chunks found for the selected videos.")
        return

    logger.info(f"Found {len(rows)} chunks to index.")

    # 3. Clear existing points for these videos in Manticore (to prevent duplicates)
    for v_id in video_ids:
        try:
            manticore.delete(m_settings.table_name, where_clause=f"video_id = {v_id}")
        except Exception as e:
            logger.warning(f"Could not delete existing points for video {v_id}: {e}")

    # 4. Generate embeddings and index
    embed_client = UnifiedEmbeddingClient(get_embedding_settings())
    batch_size = 20
    total_rows = len(rows)

    logger.info("Starting embedding generation and indexing into Manticore...")
    for i in range(0, total_rows, batch_size):
        batch_rows = rows[i : i + batch_size]
        texts = [r["text"] for r in batch_rows if r["text"] and len(r["text"].strip()) > 1]
        if not texts:
            continue

        try:
            embeddings_data = await embed_client.embed_batch_async(texts)
            points = []
            for idx, row in enumerate(batch_rows):
                dense_vec, sparse_vec = embeddings_data[idx]
                vectors: dict[str, Any] = {"default": dense_vec}
                if sparse_vec:
                    vectors["text-sparse"] = sparse_vec

                points.append(
                    {
                        "id": row["chunk_id"],
                        "vector": vectors,
                        "payload": {
                            "chunk_id": row["chunk_id"],
                            "video_id": row["video_id"],
                            "chunk_index": row["chunk_index"],
                            "start_sec": row["start_sec"],
                            "end_sec": row["end_sec"],
                            "text": row["text"],
                            "title": row["title"],
                            "source_file_id": row["source_file_id"],
                            "source_url": row["source_url"],
                            "recorded_date": date_to_int(row["recorded_date"]),
                            "is_short": bool(row["is_short"]),
                            "is_4k": bool(row["is_4k"]),
                            "is_primary": True,
                        },
                    }
                )

            if points:
                manticore.upsert(collection_name=m_settings.table_name, points=points)

            logger.info(f"Indexed chunks: {i + len(batch_rows)}/{total_rows}")

        except Exception as e:
            logger.error(f"Error processing batch starting at {i}: {e}")
            await asyncio.sleep(2)

    logger.info("Indexing completed.")

    # 5. Run a test search query
    test_query = "монтаж"
    logger.info(f"Running hybrid search query: '{test_query}'")
    with sqlite3.connect(pg_settings.db_path) as db_conn:
        db_conn.row_factory = sqlite3.Row
        results = await hybrid_search(db_conn, test_query, limit=5)

    logger.info(f"Search results for '{test_query}':")
    for idx, res in enumerate(results, 1):
        print(f"{idx}. [{res.title}] {res.start_ts} - {res.end_ts} | Score: {res.combined_score:.4f}")
        print(f"   Text: {res.text}\n")


if __name__ == "__main__":
    asyncio.run(main())
