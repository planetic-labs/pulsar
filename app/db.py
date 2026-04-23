import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from app.config import SQLiteSettings

logger = logging.getLogger(__name__)


@contextmanager
def db_connection(settings: SQLiteSettings) -> Generator[sqlite3.Connection, None, None]:
    """Provide a transactional scope around a series of operations."""
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row

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
    # 1. Videos table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_file_id TEXT NOT NULL,
            title TEXT NOT NULL,
            source_url TEXT,
            mime_type TEXT,
            size_bytes BIGINT,
            duration_sec DOUBLE PRECISION,
            local_video_path TEXT,
            local_audio_path TEXT,
            processing_status TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (source_type, source_file_id)
        )
    """)

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

    # 6. Indexes for speed
    connection.execute("CREATE INDEX IF NOT EXISTS idx_chunks_video_id ON chunks(video_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_speakers_video_id ON speakers(video_id)")

    connection.commit()
    logger.info("SQLite database simplified (only Deepgram support).")
