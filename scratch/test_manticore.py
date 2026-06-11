import asyncio

from app.manticore import get_manticore_client


async def main():
    m = get_manticore_client()

    # Check max ID
    sql_max = "SELECT max(id) FROM chunks"
    res_max = m._execute_sql(sql_max)
    print("Max ID in Manticore:", res_max)

    # Check details of ID 337273
    sql_doc = "SELECT * FROM chunks WHERE id = 337273"
    res_doc = m._execute_sql(sql_doc)
    print("Doc 337273 details:", res_doc)


if __name__ == "__main__":
    asyncio.run(main())

if __name__ == "__main__":
    asyncio.run(main())
