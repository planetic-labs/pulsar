import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_embedding_settings, get_manticore_settings, get_sqlite_settings
from app.db import db_connection, init_db
from app.embeddings import UnifiedEmbeddingClient
from app.manticore import date_to_int, get_manticore_client, init_manticore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def rebuild_semantic_index(full_reindex: bool = False):
    pg_settings = get_sqlite_settings()
    m_settings = get_manticore_settings()

    # Initialize DBs
    with db_connection(pg_settings) as conn:
        init_db(conn)

    init_manticore()
    manticore = get_manticore_client()

    embed_client = UnifiedEmbeddingClient(get_embedding_settings())

    if full_reindex:
        logger.info(f"Full reindex requested. Clearing Manticore table {m_settings.table_name}...")
        manticore.delete_collection(m_settings.table_name)
        init_manticore()

    # Automatically sync titles
    try:
        from scripts.sync_titles import sync_indexed_metadata

        await sync_indexed_metadata()
    except Exception as e:
        logger.warning(f"Could not sync titles: {e}")

    with db_connection(pg_settings) as conn:
        # Fetch all chunks with metadata
        sql = """
            SELECT
                c.id as chunk_id, c.video_id, c.chunk_index,
                c.start_sec, c.end_sec, c.text, NULL as speaker_tags,
                v.title, v.source_file_id, v.source_url, v.is_short, v.is_4k, v.recorded_date
            FROM chunks c
            JOIN videos v ON v.id = c.video_id
            ORDER BY c.id ASC
        """
        rows = conn.execute(sql).fetchall()

        if not rows:
            logger.info("No chunks found in database.")
            return

        total_rows = len(rows)
        logger.info(f"Processing {total_rows} chunks for Manticore...")

        # Batch Processing Logic
        batch_size = 50
        for i in range(0, total_rows, batch_size):
            batch_rows = rows[i : i + batch_size]

            # Check for existing points if not full reindex
            if not full_reindex:
                ids = [r["chunk_id"] for r in batch_rows]
                existing = manticore.retrieve(collection_name=m_settings.table_name, ids=ids)
                existing_ids = {p.id for p in existing}
                batch_rows = [r for r in batch_rows if r["chunk_id"] not in existing_ids]
                if not batch_rows:
                    continue

            batch_rows = [r for r in batch_rows if r["text"] and len(r["text"].strip()) > 1]
            if not batch_rows:
                continue

            texts = [r["text"] for r in batch_rows]

            try:
                # 1. Get both Dense and Sparse embeddings from unified client
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
                                "video_id": str(row["video_id"]),
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
                    # Run Manticore upsert (sync) in executor
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None, lambda p=points: manticore.upsert(collection_name=m_settings.table_name, points=p)
                    )

                logger.info(f"Progress: {i + len(batch_rows)}/{total_rows}")

            except Exception as e:
                logger.error(f"Error processing batch starting at {i}: {e}")
                await asyncio.sleep(5)

    logger.info("Indexing completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate search index to Manticore")
    parser.add_argument("--full", action="store_true", help="Clear Manticore table and start from scratch")
    args = parser.parse_args()

    asyncio.run(rebuild_semantic_index(full_reindex=args.full))
