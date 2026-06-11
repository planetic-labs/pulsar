import asyncio

from app.manticore import get_manticore_client


async def main():
    m = get_manticore_client()
    try:
        res = m._execute_sql("SHOW TABLE chunks STATUS")
        print("Manticore chunks table status:")
        if res and len(res) > 0:
            columns = [list(c.keys())[0] for c in res[0].get("columns", [])]
            data = res[0].get("data", [])
            for row in data:
                if isinstance(row, dict):
                    for k, v in row.items():
                        print(f"  {k}: {v}")
                else:
                    for k, v in zip(columns, row, strict=False):
                        print(f"  {k}: {v}")
        else:
            print("No response from Manticore")
    except Exception as e:
        print(f"Error querying table status: {e}")


if __name__ == "__main__":
    asyncio.run(main())
