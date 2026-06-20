import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from app.config import SQLiteSettings
from app.db_schema import PRAGMAS, SCHEMA_STATEMENTS, sqlite_regexp

logger = logging.getLogger(__name__)


@contextmanager
def db_connection(settings: SQLiteSettings) -> Generator[sqlite3.Connection, None, None]:
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

    # Migrations for existing databases: Add video_id, retries, and max_retries columns if they do not exist
    cursor = connection.cursor()
    cursor.execute("PRAGMA table_info(tasks)")
    columns = [row[1] for row in cursor.fetchall()]

    if "video_id" not in columns:
        try:
            connection.execute("ALTER TABLE tasks ADD COLUMN video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE")
            logger.info("Added video_id column to tasks table.")
        except Exception as e:
            logger.error(f"Error adding video_id column to tasks: {e}")

    if "retries" not in columns:
        try:
            connection.execute("ALTER TABLE tasks ADD COLUMN retries INTEGER DEFAULT 0")
            logger.info("Added retries column to tasks table.")
        except Exception as e:
            logger.error(f"Error adding retries column to tasks: {e}")

    if "max_retries" not in columns:
        try:
            connection.execute("ALTER TABLE tasks ADD COLUMN max_retries INTEGER DEFAULT 3")
            logger.info("Added max_retries column to tasks table.")
        except Exception as e:
            logger.error(f"Error adding max_retries column to tasks: {e}")

    # Migrate: add is_silent column to existing videos table
    cursor.execute("PRAGMA table_info(videos)")
    v_columns = [row[1] for row in cursor.fetchall()]
    if "is_silent" not in v_columns:
        try:
            connection.execute("ALTER TABLE videos ADD COLUMN is_silent BOOLEAN DEFAULT FALSE")
            logger.info("Added is_silent column to videos table.")
        except Exception as e:
            logger.error(f"Error adding is_silent column to videos: {e}")

    connection.commit()
    logger.info("SQLite database schema initialized.")
