import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from app.config import SQLiteSettings
from app.db_schema import PRAGMAS, SCHEMA_STATEMENTS, sqlite_regexp

logger = logging.getLogger(__name__)


@contextmanager
def db_connection(settings: SQLiteSettings) -> Generator[sqlite3.Connection]:
    """Provide a transactional scope around a series of operations."""
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(settings.db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row

    # Register custom functions
    conn.create_function("REGEXP", 2, sqlite_regexp)

    # --- PERFORMANCE TUNING ---
    for pragma in PRAGMAS:
        conn.execute(pragma)

    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(connection: sqlite3.Connection) -> None:
    """Initialize the SQLite database schema and indexes."""
    for stmt in SCHEMA_STATEMENTS:
        connection.execute(stmt)

    from app.db_schema import DB_MIGRATIONS, POST_MIGRATION_STATEMENTS

    cursor = connection.cursor()
    table_columns = {}

    for table_name, column_name, alter_sql in DB_MIGRATIONS:
        if table_name not in table_columns:
            cursor.execute(f"PRAGMA table_info({table_name})")
            table_columns[table_name] = [row[1] for row in cursor.fetchall()]

        if column_name not in table_columns[table_name]:
            try:
                connection.execute(alter_sql)
                logger.info(f"Migration: Added column '{column_name}' to table '{table_name}'.")
                table_columns[table_name].append(column_name)
            except Exception as e:
                logger.error(f"Migration failed: '{alter_sql}': {e}")

    for stmt in POST_MIGRATION_STATEMENTS:
        connection.execute(stmt)

    connection.commit()
    logger.info("SQLite database schema initialized.")
