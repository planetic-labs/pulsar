import asyncio
import logging
import sqlite3
import time

from app.config import get_embedding_settings, get_manticore_settings, get_sqlite_settings
from app.manticore import get_manticore_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def main():
    m = get_manticore_client()
    settings_m = get_manticore_settings()
    settings_s = get_sqlite_settings()
    emb_settings = get_embedding_settings()
    vector_size = emb_settings.dimension

    # 1. Load mappings from SQLite
    logger.info("Loading SQLite mapping...")
    start_time = time.perf_counter()
    sqlite_map = {}
    with sqlite3.connect("data/pulsar.db") as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT id, video_id, chunk_index FROM chunks")
        for row in cursor:
            v_id = row["video_id"]
            c_idx = row["chunk_index"]
            if v_id is not None and c_idx is not None:
                sqlite_map[(int(v_id), int(c_idx))] = row["id"]

    logger.info(f"Loaded {len(sqlite_map)} mappings from SQLite in {time.perf_counter() - start_time:.2f}s")

    # 2. Create chunks_temp in Manticore
    logger.info("Recreating chunks_temp table in Manticore...")
    m.delete_collection("chunks_temp")

    sql_chunks_temp = f"""
    CREATE TABLE IF NOT EXISTS `chunks_temp` (
        `text` text,
        `title` text,
        `speaker` text,
        `chunk_id` string,
        `video_id` string,
        `source_file_id` string,
        `source_url` string,
        `recorded_date` string,
        `chunk_index` int,
        `start_sec` float,
        `end_sec` float,
        `is_short` int,
        `is_4k` int,
        `is_primary` int,
        `vec` float_vector knn_type='hnsw' knn_dims='{vector_size}' hnsw_similarity='cosine'
    ) type='rt'
    """
    m._execute_ddl(sql_chunks_temp)
    m._execute_ddl("FLUSH TABLES")

    # 3. Read from chunks and copy to chunks_temp with corrected IDs
    logger.info("Migrating data to chunks_temp...")
    last_id = 0
    batch_size = 500  # Smaller batch size to prevent OOM
    total_read = 0
    total_written = 0
    skipped_no_vid = 0
    skipped_no_match = 0

    while True:
        sql = f"""
        SELECT id, text, title, speaker, chunk_id, video_id, source_file_id, 
               source_url, recorded_date, chunk_index, start_sec, end_sec, 
               is_short, is_4k, is_primary, vec 
        FROM chunks 
        WHERE id > {last_id} 
        ORDER BY id ASC 
        LIMIT {batch_size} 
        OPTION max_matches={batch_size}
        """
        try:
            res = m._execute_sql(sql)
        except Exception as e:
            logger.error(f"Error querying Manticore at last_id={last_id}: {e}")
            logger.info("Waiting 5s and retrying...")
            await asyncio.sleep(5)
            continue

        if not res or len(res) == 0:
            break

        data = res[0].get("data", [])
        if not data:
            break

        columns = [list(c.keys())[0] for c in res[0].get("columns", [])]

        points_to_insert = []
        for row in data:
            if isinstance(row, dict):
                row_dict = row
            else:
                row_dict = dict(zip(columns, row))

            doc_id = row_dict["id"]
            last_id = max(last_id, doc_id)
            total_read += 1

            v_id_str = row_dict.get("video_id")
            c_idx = row_dict.get("chunk_index")

            if not v_id_str:
                skipped_no_vid += 1
                continue

            try:
                v_id = int(v_id_str)
                c_idx = int(c_idx)
            except (ValueError, TypeError):
                skipped_no_match += 1
                continue

            sqlite_id = sqlite_map.get((v_id, c_idx))
            if sqlite_id is None:
                skipped_no_match += 1
                continue

            # Safely parse vector
            vec_val = row_dict.get("vec")
            if isinstance(vec_val, str):
                vec_list = [float(x) for x in vec_val.split(",") if x.strip()]
            elif isinstance(vec_val, list):
                vec_list = [float(x) for x in vec_val]
            else:
                vec_list = [0.0] * vector_size

            points_to_insert.append(
                {
                    "id": sqlite_id,
                    "vector": vec_list,
                    "payload": {
                        "text": row_dict.get("text") or "",
                        "title": row_dict.get("title") or "",
                        "speaker": row_dict.get("speaker") or "",
                        "chunk_id": str(sqlite_id),
                        "video_id": str(v_id),
                        "source_file_id": row_dict.get("source_file_id") or "",
                        "source_url": row_dict.get("source_url") or "",
                        "recorded_date": row_dict.get("recorded_date") or "",
                        "chunk_index": int(c_idx),
                        "start_sec": float(row_dict.get("start_sec") or 0.0),
                        "end_sec": float(row_dict.get("end_sec") or 0.0),
                        "is_short": int(row_dict.get("is_short") or 0),
                        "is_4k": int(row_dict.get("is_4k") or 0),
                        "is_primary": int(row_dict.get("is_primary") or 1),
                    },
                }
            )

        if points_to_insert:
            for attempt in range(5):
                try:
                    m.upsert("chunks_temp", points_to_insert)
                    total_written += len(points_to_insert)
                    break
                except Exception as e:
                    logger.error(f"Error writing to Manticore chunks_temp (attempt {attempt + 1}/5): {e}")
                    if attempt == 4:
                        raise
                    logger.info("Waiting 5s before retrying upsert...")
                    await asyncio.sleep(5)

        logger.info(f"Processed {total_read} docs... Matched and written: {total_written}")

        # Flush every 5,000 writes to save RAM HNSW graph
        if total_written > 0 and total_written % 5000 == 0:
            logger.info("Flushing chunks_temp to disk...")
            m._execute_ddl("FLUSH TABLES")

        # Give Manticore room to breathe
        await asyncio.sleep(0.05)

        if len(data) < batch_size:
            break

    # 4. Atomic swap
    logger.info("\nData migration finished.")
    logger.info(f"Total read from Manticore: {total_read}")
    logger.info(f"Total written to chunks_temp: {total_written}")
    logger.info(f"Skipped (no video_id): {skipped_no_vid}")
    logger.info(f"Skipped (no SQLite match): {skipped_no_match}")

    if total_written > 0:
        logger.info("Dropping old chunks table...")
        m.delete_collection("chunks")
        logger.info("Renaming chunks_temp to chunks...")
        m._execute_ddl("RENAME TABLE chunks_temp TO chunks")
        m._execute_ddl("FLUSH TABLES")
        logger.info("Manticore IDs successfully corrected and optimized!")
    else:
        logger.error("No records were migrated! Aborting swap.")


if __name__ == "__main__":
    asyncio.run(main())
