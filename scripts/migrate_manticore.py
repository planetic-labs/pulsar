import asyncio
import logging
import re
import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_manticore_settings, get_sqlite_settings
from app.manticore import get_manticore_client, init_manticore
from scripts.load_all_for_quotes import index_sqlite_to_manticore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def verify_schema(client, table_name: str) -> tuple[bool, str]:
    """
    Verifies that the existing table schema matches the target architecture:
    1. Table must exist.
    2. 'vec' column must NOT exist.
    3. 'speaker' column must NOT exist.
    4. 'chunk_id' column must have 'bigint' type.
    5. Table option 'morphology' must be 'stem_ru' (and NOT 'stem_ru, stem_en').
    """
    # Check if table exists
    try:
        tables = client._execute_sql("SHOW TABLES")
        table_exists = False
        if tables and len(tables) > 0:
            for row in tables[0].get("data", []):
                # Format can be dict or list depending on query mode
                t_name = row.get("Table") if isinstance(row, dict) else row[0]
                if t_name == table_name:
                    table_exists = True
                    break
        if not table_exists:
            return False, f"Table '{table_name}' does not exist."
    except Exception as e:
        return False, f"Failed to check tables: {e}"

    # Describe table columns
    try:
        desc = client._execute_sql(f"DESCRIBE {table_name}")
        columns = {}
        if desc and len(desc) > 0:
            for row in desc[0].get("data", []):
                field = row.get("Field")
                t_type = row.get("Type")
                columns[field] = t_type

        # Check vec
        if "vec" in columns:
            return False, "Column 'vec' still exists in schema."

        # Check speaker
        if "speaker" in columns:
            return False, "Column 'speaker' still exists in schema."

        # Check chunk_id
        if "chunk_id" not in columns:
            return False, "Column 'chunk_id' is missing."
        if columns["chunk_id"] != "bigint":
            return False, f"Column 'chunk_id' has type '{columns['chunk_id']}', expected 'bigint'."

    except Exception as e:
        return False, f"Failed to describe table '{table_name}': {e}"

    # Verify table settings (morphology, rt_mem_limit)
    try:
        create_table_res = client._execute_sql(f"SHOW CREATE TABLE {table_name}")
        if create_table_res and len(create_table_res) > 0:
            create_sql = create_table_res[0].get("data", [])[0]
            # Handle dict/list format
            create_sql_str = create_sql.get("Create Table") if isinstance(create_sql, dict) else create_sql[1]

            # Check morphology (case insensitive search)
            morph_match = re.search(r"morphology='([^']+)'", create_sql_str, re.IGNORECASE)
            if not morph_match:
                return False, "Morphology setting is missing in table options."

            morph_val = morph_match.group(1).lower()
            if morph_val != "stem_ru":
                return False, f"Morphology is '{morph_val}', expected 'stem_ru'."

    except Exception as e:
        return False, f"Failed to fetch CREATE TABLE for '{table_name}': {e}"

    return True, "Schema is up to date."


async def main():
    m_settings = get_manticore_settings()
    sqlite_settings = get_sqlite_settings()
    client = get_manticore_client()
    table_name = m_settings.table_name.lower()

    logger.info(f"Verifying schema for table '{table_name}'...")
    is_valid, reason = verify_schema(client, table_name)

    if is_valid:
        logger.info(f"✅ Manticore Search schema is up to date: {reason}")
        return

    logger.warning(f"⚠️ Manticore Search schema mismatch detected: {reason}")
    logger.info("Starting database schema migration...")

    # 1. Drop old table
    logger.info(f"Dropping table '{table_name}'...")
    client.delete_collection(table_name)

    # 2. Re-create table with new schema
    logger.info("Re-creating Manticore table with target DDL...")
    init_manticore()

    logger.info("Waiting for table initialization to bake...")
    await asyncio.sleep(2)

    # 3. Re-index all data
    logger.info("Starting database re-indexing...")
    await index_sqlite_to_manticore(client, table_name, sqlite_settings.db_path)
    logger.info("🎉 Manticore Search schema migration and indexing completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
