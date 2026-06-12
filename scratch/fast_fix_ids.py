import asyncio
import sqlite3
import time

from app.config import get_manticore_settings, get_sqlite_settings
from app.manticore import get_manticore_client


async def main():
    m = get_manticore_client()
    settings_m = get_manticore_settings()
    settings_s = get_sqlite_settings()

    print("Loading SQLite mapping...")
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

    print(f"Loaded {len(sqlite_map)} mappings from SQLite in {time.perf_counter() - start_time:.2f}s")

    # Read from Manticore using keyset pagination
    print("Reading from Manticore...")
    start_time = time.perf_counter()
    last_id = 0
    batch_size = 5000
    total_read = 0
    matched = 0
    skipped_no_vid = 0
    skipped_no_match = 0

    while True:
        # Select all columns including vec (vector)
        sql = f"SELECT id, video_id, chunk_index, text, vec FROM chunks WHERE id > {last_id} ORDER BY id ASC LIMIT {batch_size} OPTION max_matches={batch_size}"
        res = m._execute_sql(sql)
        if not res or len(res) == 0:
            break

        data = res[0].get("data", [])
        if not data:
            break

        columns = [list(c.keys())[0] for c in res[0].get("columns", [])]

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

            if not v_id_str or c_idx is None:
                skipped_no_vid += 1
                continue

            try:
                v_id = int(v_id_str)
                c_idx = int(c_idx)
            except (ValueError, TypeError):
                skipped_no_match += 1
                continue

            sqlite_id = sqlite_map.get((v_id, c_idx))
            if sqlite_id is not None:
                matched += 1
            else:
                skipped_no_match += 1

        print(f"Processed {total_read} documents... Matched: {matched}")
        if len(data) < batch_size:
            break

    print(f"\nCompleted reading Manticore in {time.perf_counter() - start_time:.2f}s")
    print(f"Total read from Manticore: {total_read}")
    print(f"Matched with SQLite: {matched}")
    print(f"Skipped (no video_id): {skipped_no_vid}")
    print(f"Skipped (no SQLite match): {skipped_no_match}")


if __name__ == "__main__":
    asyncio.run(main())
