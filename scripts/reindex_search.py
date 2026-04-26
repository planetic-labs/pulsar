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

from qdrant_client import models

from app.config import get_embedding_settings, get_qdrant_settings, get_sqlite_settings
from app.db import db_connection, init_db
from app.gemini import UnifiedEmbeddingClient
from app.qdrant import get_qdrant_client, init_qdrant

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Google AI Free Tier Limits (now handled by client, but kept for legacy ratio)
CHAR_TO_TOKEN_RATIO = 0.3


async def rebuild_semantic_index(full_reindex: bool = False):
    pg_settings = get_sqlite_settings()
    q_settings = get_qdrant_settings()

    # Initialize DBs
    with db_connection(pg_settings) as conn:
        init_db(conn)

    init_qdrant()
    qdrant = get_qdrant_client()

    embed_client = UnifiedEmbeddingClient(get_embedding_settings())

    if full_reindex:
        logger.info(f"Full reindex requested. Clearing Qdrant collection {q_settings.collection_name}...")
        qdrant.delete_collection(q_settings.collection_name)
        init_qdrant()

    # Automatically sync titles
    try:
        from scripts.sync_titles import sync_indexed_metadata

        await sync_indexed_metadata()
    except Exception as e:
        logger.warning(f"Could not sync titles: {e}")

    with db_connection(pg_settings) as conn:
        # Fetch all chunks with metadata (engine/is_primary removed)
        sql = """
            SELECT
                c.id as chunk_id, c.video_id, c.transcript_id, c.chunk_index,
                c.start_sec, c.end_sec, c.text, c.speaker_tags,
                v.title, v.source_file_id, v.source_url
            FROM chunks c
            JOIN videos v ON v.id = c.video_id
            ORDER BY c.id ASC
        """
        rows = conn.execute(sql).fetchall()

        if not rows:
            logger.info("No chunks found in database.")
            return

        total_rows = len(rows)
        logger.info(f"Processing {total_rows} chunks for Qdrant...")

        # New Batch Processing Logic
        batch_size = 50
        for i in range(0, total_rows, batch_size):
            batch_rows = rows[i : i + batch_size]

            # Check for existing points if not full reindex
            if not full_reindex:
                ids = [r["chunk_id"] for r in batch_rows]
                existing = qdrant.retrieve(collection_name=q_settings.collection_name, ids=ids)
                existing_ids = {p.id for p in existing}
                batch_rows = [r for r in batch_rows if r["chunk_id"] not in existing_ids]
                if not batch_rows:
                    continue

            texts = [r["text"] for r in batch_rows if r["text"] and len(r["text"].strip()) > 1]
            if not texts:
                continue

            try:
                # 1. Get both Dense and Sparse embeddings from unified client
                embeddings_data = await embed_client.embed_batch_async(texts)

                points: list[models.PointStruct] = []
                for idx, row in enumerate(batch_rows):
                    dense_vec, sparse_vec = embeddings_data[idx]

                    vectors: dict[str, Any] = {"default": dense_vec, "text-sparse": sparse_vec}
                    points.append(
                        models.PointStruct(
                            id=row["chunk_id"],
                            vector=vectors,
                            payload={
                                "chunk_id": row["chunk_id"],
                                "video_id": row["video_id"],
                                "transcript_id": row["transcript_id"],
                                "chunk_index": row["chunk_index"],
                                "start_sec": row["start_sec"],
                                "end_sec": row["end_sec"],
                                "text": row["text"],
                                "speaker": row["speaker_tags"],
                                "title": row["title"],
                                "source_file_id": row["source_file_id"],
                                "source_url": row["source_url"],
                                "is_primary": True,
                            },
                        )
                    )

                if points:
                    # Run Qdrant upsert (sync) in executor
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None, lambda p=points: qdrant.upsert(collection_name=q_settings.collection_name, points=p)
                    )

                logger.info(f"Progress: {i + len(batch_rows)}/{total_rows}")

            except Exception as e:
                logger.error(f"Error processing batch starting at {i}: {e}")
                await asyncio.sleep(5)

    logger.info("Indexing completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate search index to Qdrant")
    parser.add_argument("--full", action="store_true", help="Clear Qdrant collection and start from scratch")
    args = parser.parse_args()

    asyncio.run(rebuild_semantic_index(full_reindex=args.full))
