import asyncio
import logging

from app.config import get_manticore_settings, get_sqlite_settings
from app.db import db_connection
from app.manticore import date_to_int, get_manticore_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    m = get_manticore_client()
    m_settings = get_manticore_settings()
    s_settings = get_sqlite_settings()

    logger.info("Fetching all metadata from SQLite for chunks in range...")
    sqlite_data = {}
    with db_connection(s_settings) as conn:
        sql = """
            SELECT 
                c.id as chunk_id, c.video_id, c.chunk_index, c.start_sec, c.end_sec, c.text,
                v.title, v.source_file_id, v.source_url, v.recorded_date, v.is_short, v.is_4k
            FROM chunks c
            JOIN videos v ON v.id = c.video_id
            WHERE c.id BETWEEN 167930 AND 168000
        """
        cursor = conn.execute(sql)
        for row in cursor:
            sqlite_data[row["chunk_id"]] = dict(row)

    logger.info(f"Loaded {len(sqlite_data)} mappings from SQLite.")

    # Fetch vectors from Manticore
    logger.info("Reading vectors from Manticore...")
    res = m._execute_sql("SELECT id, vec FROM chunks WHERE id >= 167930 AND id <= 168000 LIMIT 100")

    if not res or not res[0].get("data"):
        logger.warning("No documents found in specified range in Manticore.")
        return

    data = res[0]["data"]
    columns = [list(c.keys())[0] for c in res[0]["columns"]]

    points_to_update = []
    for row in data:
        if isinstance(row, dict):
            r = row
        else:
            r = dict(zip(columns, row))

        doc_id = int(r["id"])
        meta = sqlite_data.get(doc_id)
        if not meta:
            logger.warning(f"No SQLite metadata for chunk {doc_id}")
            continue

        # Parse vector
        vec_val = r.get("vec")
        if isinstance(vec_val, str):
            vec_list = [float(x) for x in vec_val.split(",") if x.strip()]
        elif isinstance(vec_val, list):
            vec_list = [float(x) for x in vec_val]
        else:
            logger.warning(f"No vector found for chunk {doc_id}")
            vec_list = [0.0] * 4096

        logger.info(f"Preparing update for chunk {doc_id}: video_id={meta['video_id']}, title={meta['title']!r}")

        points_to_update.append(
            {
                "id": doc_id,
                "vector": vec_list,
                "payload": {
                    "chunk_id": doc_id,
                    "video_id": str(meta["video_id"]),
                    "chunk_index": int(meta["chunk_index"]),
                    "start_sec": float(meta["start_sec"] or 0.0),
                    "end_sec": float(meta["end_sec"] or 0.0),
                    "text": meta["text"] or "",
                    "title": meta["title"] or "",
                    "source_file_id": meta["source_file_id"] or "",
                    "source_url": meta["source_url"] or "",
                    "recorded_date": date_to_int(meta["recorded_date"]),
                    "is_short": int(meta["is_short"] or 0),
                    "is_4k": int(meta["is_4k"] or 0),
                    "is_primary": 1,
                },
            }
        )

    if points_to_update:
        logger.info(f"Upserting {len(points_to_update)} updated documents to Manticore...")
        m.upsert(m_settings.table_name, points_to_update)
        logger.info("Successfully fixed empty video_ids in Manticore!")
    else:
        logger.info("No documents needed updates.")


if __name__ == "__main__":
    asyncio.run(main())
