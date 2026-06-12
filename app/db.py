import logging
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from app.config import SQLiteSettings

logger = logging.getLogger(__name__)


def sqlite_regexp(expr: str, item: str | None) -> bool:
    """Custom REGEXP implementation for SQLite."""
    if item is None:
        return False
    try:
        return re.search(expr, item, re.IGNORECASE) is not None
    except Exception:
        return False


@contextmanager
def db_connection(settings: SQLiteSettings) -> Generator[sqlite3.Connection, None, None]:
    """Provide a transactional scope around a series of operations."""
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row

    # Register custom functions
    conn.create_function("REGEXP", 2, sqlite_regexp)

    # --- PERFORMANCE TUNING ---
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA mmap_size=1073741824;")
    conn.execute("PRAGMA cache_size=-102400;")
    conn.execute("PRAGMA foreign_keys=ON;")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(connection: sqlite3.Connection) -> None:
    """Initialize the SQLite database schema and indexes."""
    # 0. Folders table (NEW: for directory hierarchy)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            parent_id TEXT REFERENCES folders(id) ON DELETE CASCADE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CHECK (parent_id IS NULL OR parent_id != id)
        )
    """)

    # 1. Videos table (UPDATED: removed source_type, local paths, unique on source_file_id)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file_id TEXT NOT NULL,
            parent_folder_id TEXT REFERENCES folders(id) ON DELETE SET NULL,
            md5_checksum TEXT,
            title TEXT NOT NULL,
            recorded_date DATE,
            is_short BOOLEAN DEFAULT FALSE,
            source_url TEXT,
            mime_type TEXT,
            size_bytes BIGINT,
            duration_sec DOUBLE PRECISION,
            status TEXT NOT NULL,
            is_4k BOOLEAN DEFAULT FALSE,
            is_missing BOOLEAN DEFAULT FALSE,
            is_excluded BOOLEAN DEFAULT FALSE,
            original_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (source_file_id),
            CHECK (original_id IS NULL OR original_id != id),
            CHECK (size_bytes IS NULL OR size_bytes >= 0),
            CHECK (duration_sec IS NULL OR duration_sec >= 0)
        )
    """)

    # 3. Chunks table (UPDATED: removed speaker_tags, added UNIQUE and CHECK constraints)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            start_sec DOUBLE PRECISION NOT NULL,
            end_sec DOUBLE PRECISION NOT NULL,
            text TEXT NOT NULL,
            UNIQUE(video_id, chunk_index),
            CHECK(chunk_index >= 0),
            CHECK(start_sec >= 0),
            CHECK(end_sec >= start_sec),
            CHECK(length(trim(text)) > 0)
        )
    """)

    # 4. Tasks table (Queue)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
            task_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER DEFAULT 0,
            retries INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            error_message TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

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

    # 5. Query cache table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS query_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT UNIQUE NOT NULL,
            dense_vector BLOB NOT NULL,
            sparse_indices BLOB,
            sparse_values BLOB,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 6. Indexes for speed
    connection.execute("CREATE INDEX IF NOT EXISTS idx_videos_parent_folder ON videos(parent_folder_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_videos_md5 ON videos(md5_checksum)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_query_cache_query ON query_cache(query)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_videos_original_id ON videos(original_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_folders_parent_name ON folders(parent_id, name)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(status, priority DESC, created_at ASC)")

    # Partial unique index to guarantee only one original video exists per md5_checksum
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uidx_videos_md5_original
        ON videos(md5_checksum)
        WHERE original_id IS NULL AND md5_checksum IS NOT NULL AND md5_checksum != ''
    """)

    # Trigger to automatically update updated_at on updates
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_videos_updated_at
        AFTER UPDATE ON videos
        BEGIN
            UPDATE videos SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
        END;
    """)

    # Trigger to automatically update updated_at on updates in tasks
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_tasks_updated_at
        AFTER UPDATE ON tasks
        BEGIN
            UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
        END;
    """)

    # Triggers to prevent duplicate chains (copy of a copy)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_videos_prevent_duplicate_chains_insert
        BEFORE INSERT ON videos
        FOR EACH ROW
        WHEN NEW.original_id IS NOT NULL
        BEGIN
            SELECT CASE
                WHEN (SELECT original_id FROM videos WHERE id = NEW.original_id) IS NOT NULL
                THEN RAISE(ABORT, 'Циклическая или многоуровневая копия: оригинальный файл сам является дубликатом!')
            END;
        END;
    """)

    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_videos_prevent_duplicate_chains_update
        BEFORE UPDATE ON videos
        FOR EACH ROW
        WHEN NEW.original_id IS NOT NULL
        BEGIN
            SELECT CASE
                WHEN (SELECT original_id FROM videos WHERE id = NEW.original_id) IS NOT NULL
                THEN RAISE(ABORT, 'Циклическая или многоуровневая копия: оригинальный файл сам является дубликатом!')
            END;
        END;
    """)

    # Trigger to prevent infinite loops in folder tree hierarchies (A -> B -> C -> A)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_folders_prevent_loops
        BEFORE UPDATE ON folders
        FOR EACH ROW
        WHEN NEW.parent_id IS NOT NULL
        BEGIN
            SELECT CASE
                WHEN EXISTS (
                    WITH RECURSIVE path(id, parent_id) AS (
                        SELECT id, parent_id FROM folders WHERE id = NEW.parent_id
                        UNION ALL
                        SELECT f.id, f.parent_id FROM folders f JOIN path p ON f.id = p.parent_id
                    )
                    SELECT 1 FROM path WHERE id = NEW.id
                ) THEN RAISE(ABORT, 'Циклическая зависимость: папка не может быть вложена в своего потомка!')
            END;
        END;
    """)

    # 8. Revoked sessions table (for JWT revocation)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS revoked_sessions (
            jti TEXT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 9. Revoked users table (for user revocation)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS revoked_users (
            user_id TEXT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 10. Integrity issues table (for verify_integrity.py results)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS integrity_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.commit()
    logger.info("SQLite database schema initialized.")
