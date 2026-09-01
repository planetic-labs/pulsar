import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.chunking import CHUNKING_ALGORITHM_VERSION, get_chunking_config_hash
from app.config import get_embedding_settings, get_manticore_settings, get_sqlite_settings
from app.db import db_connection, init_db
from app.embeddings import UnifiedEmbeddingClient
from app.indexing_state import (
    backfill_chunk_metadata,
    ensure_active_generation,
    generation_name,
)
from app.manticore import date_to_int, get_manticore_client, init_manticore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _count_table(manticore: Any, table_name: str) -> int:
    response = manticore._execute_sql(f"SELECT COUNT(*) AS count FROM `{table_name}`")
    row = response[0]["data"][0]
    if isinstance(row, dict):
        return int(row.get("count(*)", row.get("count", 0)))
    return int(row[0])


def _promote_staging_table(manticore: Any, active: str, staging: str, retired: str) -> None:
    """Promote a validated table, restoring the old name if promotion fails."""
    manticore._execute_ddl(f"ALTER TABLE `{active}` RENAME `{retired}`")
    try:
        manticore._execute_ddl(f"ALTER TABLE `{staging}` RENAME `{active}`")
    except Exception:
        logger.exception("Promotion failed; restoring previous active Manticore table")
        manticore._execute_ddl(f"ALTER TABLE `{retired}` RENAME `{active}`")
        raise


async def rebuild_semantic_index(full_reindex: bool = False) -> None:
    pg_settings = get_sqlite_settings()
    m_settings = get_manticore_settings()

    # Initialize DBs
    with db_connection(pg_settings) as conn:
        init_db(conn)

    manticore = get_manticore_client()
    embed_client = UnifiedEmbeddingClient(get_embedding_settings())
    target_table = m_settings.table_name
    build_generation_id: int | None = None

    if full_reindex:
        suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        target_table = f"{m_settings.table_name}_build_{suffix}"
        logger.info("Full reindex will build isolated staging table %s", target_table)
        with db_connection(pg_settings) as conn:
            active_row = conn.execute(
                "SELECT id FROM index_generations WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            active_generation_id = int(active_row["id"]) if active_row else ensure_active_generation(conn)
            backfill_chunk_metadata(conn, active_generation_id)
            count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            embedding = get_embedding_settings()
            cursor = conn.execute(
                """
                INSERT INTO index_generations (
                    name, status, chunking_version, config_hash, embedding_model,
                    embedding_dimension, manticore_table, expected_chunks
                ) VALUES (?, 'building', ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{generation_name()}-{suffix}",
                    CHUNKING_ALGORITHM_VERSION,
                    get_chunking_config_hash(),
                    embedding.model_id,
                    embedding.dimension,
                    target_table,
                    count,
                ),
            )
            assert cursor.lastrowid is not None
            build_generation_id = int(cursor.lastrowid)
        init_manticore(target_table)
    else:
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

            # Check for existing points only during an incremental run.
            if not full_reindex:
                ids = [r["chunk_id"] for r in batch_rows]
                existing = manticore.retrieve(collection_name=target_table, ids=ids)
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
                        None, lambda p=points: manticore.upsert(collection_name=target_table, points=p)
                    )

                logger.info(f"Progress: {i + len(batch_rows)}/{total_rows}")

            except Exception as e:
                logger.error(f"Error processing batch starting at {i}: {e}")
                if build_generation_id is not None:
                    with db_connection(pg_settings) as conn:
                        conn.execute(
                            """
                            UPDATE index_generations
                            SET status = 'failed', error_count = error_count + 1
                            WHERE id = ?
                            """,
                            (build_generation_id,),
                        )
                raise

    if full_reindex and build_generation_id is not None:
        actual_count = _count_table(manticore, target_table)
        if actual_count != total_rows:
            with db_connection(pg_settings) as conn:
                conn.execute(
                    "UPDATE index_generations SET status = 'failed', indexed_chunks = ?, error_count = 1 WHERE id = ?",
                    (actual_count, build_generation_id),
                )
            raise RuntimeError(f"staging validation failed: expected {total_rows} documents, got {actual_count}")

        retired_table = f"{m_settings.table_name}_retired_{build_generation_id}"
        _promote_staging_table(manticore, m_settings.table_name, target_table, retired_table)
        with db_connection(pg_settings) as conn:
            conn.execute("UPDATE index_generations SET status = 'retired' WHERE status = 'active'")
            conn.execute(
                """
                UPDATE index_generations
                SET status = 'active', manticore_table = ?, indexed_chunks = ?, activated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (m_settings.table_name, actual_count, build_generation_id),
            )
            conn.execute("UPDATE chunks SET generation_id = ?", (build_generation_id,))
        marker = ROOT_DIR / "data" / "REINDEX_REQUIRED"
        marker.unlink(missing_ok=True)
        logger.info(
            "Promoted generation %s. Previous table is retained as %s for rollback.",
            build_generation_id,
            retired_table,
        )

    logger.info("Indexing completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate search index to Manticore")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Build and validate an isolated table, then promote it while retaining the previous table",
    )
    args = parser.parse_args()

    asyncio.run(rebuild_semantic_index(full_reindex=args.full))
