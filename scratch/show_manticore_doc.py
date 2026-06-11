import asyncio

from app.manticore import get_manticore_client


async def main():
    m = get_manticore_client()
    sql = "SELECT id, chunk_index, text FROM chunks WHERE video_id = '7017' LIMIT 10"
    res = m._execute_sql(sql)
    print("Chunks of video 7017 in Manticore:")
    if res and len(res) > 0:
        data = res[0].get("data", [])
        for row in data:
            if isinstance(row, dict):
                print(f"  ID: {row['id']}, Chunk Index: {row['chunk_index']}, Text: {row['text'][:100]}...")
            else:
                columns = [list(c.keys())[0] for c in res[0].get("columns", [])]
                row_dict = dict(zip(columns, row))
                print(
                    f"  ID: {row_dict['id']}, Chunk Index: {row_dict['chunk_index']}, Text: {row_dict['text'][:100]}..."
                )
    else:
        print("No response")


if __name__ == "__main__":
    asyncio.run(main())
