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
            parent_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 1. Videos table (UPDATED: added md5_checksum)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_file_id TEXT NOT NULL,
            parent_folder_id TEXT,
            md5_checksum TEXT,
            title TEXT NOT NULL,
            recorded_date DATE,
            is_short BOOLEAN DEFAULT FALSE,
            source_url TEXT,
            mime_type TEXT,
            size_bytes BIGINT,
            duration_sec DOUBLE PRECISION,
            local_video_path TEXT,
            local_audio_path TEXT,
            processing_status TEXT NOT NULL,
            is_4k BOOLEAN DEFAULT FALSE,
            is_missing BOOLEAN DEFAULT FALSE,
            is_excluded BOOLEAN DEFAULT FALSE,
            is_md5_duplicate BOOLEAN DEFAULT FALSE,
            is_md5_duplicate_saved BOOLEAN DEFAULT FALSE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (source_type, source_file_id)
        )
    """)

    # --- MIGRATIONS ---
    cursor = connection.execute("PRAGMA table_info(videos)")
    columns = [row["name"] for row in cursor.fetchall()]
    if "parent_folder_id" not in columns:
        logger.info("Migrating database: Adding parent_folder_id column to videos table.")
        connection.execute("ALTER TABLE videos ADD COLUMN parent_folder_id TEXT")

    if "recorded_date" not in columns:
        logger.info("Migrating database: Adding recorded_date column to videos table.")
        connection.execute("ALTER TABLE videos ADD COLUMN recorded_date DATE")

    if "is_short" not in columns:
        logger.info("Migrating database: Adding is_short column to videos table.")
        connection.execute("ALTER TABLE videos ADD COLUMN is_short BOOLEAN DEFAULT FALSE")

    if "md5_checksum" not in columns:
        logger.info("Migrating database: Adding md5_checksum column to videos table.")
        connection.execute("ALTER TABLE videos ADD COLUMN md5_checksum TEXT")

    if "is_4k" not in columns:
        logger.info("Migrating database: Adding is_4k column to videos table.")
        connection.execute("ALTER TABLE videos ADD COLUMN is_4k BOOLEAN DEFAULT FALSE")

    if "is_missing" not in columns:
        logger.info("Migrating database: Adding is_missing column to videos table.")
        connection.execute("ALTER TABLE videos ADD COLUMN is_missing BOOLEAN DEFAULT FALSE")

    if "is_excluded" not in columns:
        logger.info("Migrating database: Adding is_excluded column to videos table.")
        connection.execute("ALTER TABLE videos ADD COLUMN is_excluded BOOLEAN DEFAULT FALSE")

    if "is_md5_duplicate" not in columns:
        logger.info("Migrating database: Adding is_md5_duplicate column to videos table.")
        connection.execute("ALTER TABLE videos ADD COLUMN is_md5_duplicate BOOLEAN DEFAULT FALSE")

    if "is_md5_duplicate_saved" not in columns:
        logger.info("Migrating database: Adding is_md5_duplicate_saved column to videos table.")
        connection.execute("ALTER TABLE videos ADD COLUMN is_md5_duplicate_saved BOOLEAN DEFAULT FALSE")

    # 2. Transcripts table (SIMPLIFIED: engine removed)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            language TEXT NOT NULL,
            confidence DOUBLE PRECISION,
            raw_json_path TEXT,
            normalized_json_path TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 3. Speakers table (NEW: Speaker reference)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS speakers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            speaker_tag TEXT NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(video_id, speaker_tag)
        )
    """)

    # 4. Chunks table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            start_sec DOUBLE PRECISION NOT NULL,
            end_sec DOUBLE PRECISION NOT NULL,
            text TEXT NOT NULL,
            speaker_tags TEXT
        )
    """)

    # 5. Tasks table (Queue)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER DEFAULT 0,
            error_message TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 6. Query cache table
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

    # 7. Indexes for speed
    connection.execute("CREATE INDEX IF NOT EXISTS idx_chunks_video_id ON chunks(video_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_speakers_video_id ON speakers(video_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_videos_parent_folder ON videos(parent_folder_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_videos_md5 ON videos(md5_checksum)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_query_cache_query ON query_cache(query)")

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

    connection.commit()
    logger.info("SQLite database schema initialized.")
