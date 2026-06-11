import asyncio
import json

import httpx

from app.manticore import get_manticore_client


async def main():
    m = get_manticore_client()

    # Let's insert a test document with a specific large ID, say 999999
    test_id = 999999
    doc = {
        "text": "тестовый текст для проверки ID в Manticore",
        "video_id": "999",
        "chunk_index": 99,
        "start_sec": 9.9,
        "end_sec": 9.9,
        "recorded_date": 0,
        "is_short": 0,
        "is_4k": 0,
        "speaker": "",
    }

    # 1. Try with "table"
    line_table = {"replace": {"table": "chunks", "id": test_id, "doc": doc}}
    body_table = json.dumps(line_table) + "\n"

    print("Testing insert with 'table'...")
    r = httpx.post(
        f"{m.url}/json/bulk", content=body_table.encode("utf-8"), headers={"Content-Type": "application/x-ndjson"}
    )
    print("Response status:", r.status_code)
    print("Response text:", r.text)

    # Verify if ID 999999 exists
    res = m._execute_sql("SELECT id, text FROM chunks WHERE id = 999999")
    print("Verification (SELECT where id=999999):", res)

    # Clean up if it was inserted
    m._execute_ddl("DELETE FROM chunks WHERE id = 999999")


if __name__ == "__main__":
    asyncio.run(main())
