from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def sqlite_regexp(expr: str, item: str | None) -> bool:
    """Реализация REGEXP для SQLite."""
    if item is None:
        return False
    try:
        return re.search(expr, item, re.IGNORECASE) is not None
    except re.error:
        return False


# Настройки производительности SQLite (PRAGMAs)
PRAGMAS = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA mmap_size=1073741824;",
    "PRAGMA cache_size=-102400;",
    "PRAGMA foreign_keys=ON;",
]

# Описание таблиц, индексов и триггеров
SCHEMA_STATEMENTS = [
    # 0. Folders table
    """
    CREATE TABLE IF NOT EXISTS folders (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        parent_id TEXT REFERENCES folders(id) ON DELETE CASCADE,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        CHECK (parent_id IS NULL OR parent_id != id)
    );
    """,
    # 1. Videos table
    """
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
        is_silent BOOLEAN DEFAULT FALSE,
        original_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (source_file_id),
        CHECK (original_id IS NULL OR original_id != id),
        CHECK (size_bytes IS NULL OR size_bytes >= 0),
        CHECK (duration_sec IS NULL OR duration_sec >= 0)
    );
    """,
    # 2. Chunks table
    """
    CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        start_sec DOUBLE PRECISION NOT NULL,
        end_sec DOUBLE PRECISION NOT NULL,
        text TEXT NOT NULL,
        logical_id TEXT,
        content_hash TEXT,
        chunking_version TEXT NOT NULL DEFAULT 'legacy',
        generation_id INTEGER,
        UNIQUE(video_id, chunk_index),
        CHECK(chunk_index >= 0),
        CHECK(start_sec >= 0),
        CHECK(end_sec >= start_sec),
        CHECK(length(trim(text)) > 0)
    );
    """,
    # 3. Tasks table
    """
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
        dedupe_key TEXT,
        next_attempt_at DATETIME,
        failure_kind TEXT,
        generation_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # 4. Query cache table
    """
    CREATE TABLE IF NOT EXISTS query_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT UNIQUE NOT NULL,
        dense_vector BLOB NOT NULL,
        sparse_indices BLOB,
        sparse_values BLOB,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # 5. Revoked sessions table
    """
    CREATE TABLE IF NOT EXISTS revoked_sessions (
        jti TEXT PRIMARY KEY,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # 6. Revoked users table
    """
    CREATE TABLE IF NOT EXISTS revoked_users (
        user_id TEXT PRIMARY KEY,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # 7. Integrity issues table
    """
    CREATE TABLE IF NOT EXISTS integrity_issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # 8. Subtitle flags table (moderation queue)
    """
    CREATE TABLE IF NOT EXISTS subtitle_flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        locked_by TEXT,
        locked_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(chunk_id)
    );
    """,
    # 9. Search history table
    """
    CREATE TABLE IF NOT EXISTS search_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        query TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, query)
    );
    """,
    # 10. Versioned search index builds. Only validated generations may become active.
    """
    CREATE TABLE IF NOT EXISTS index_generations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('building', 'ready', 'active', 'failed', 'retired')),
        chunking_version TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        embedding_model TEXT NOT NULL,
        embedding_dimension INTEGER NOT NULL,
        manticore_table TEXT NOT NULL,
        expected_chunks INTEGER NOT NULL DEFAULT 0,
        indexed_chunks INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        activated_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # 11. Transactional outbox connecting SQLite mutations to Manticore updates.
    """
    CREATE TABLE IF NOT EXISTS index_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL CHECK(event_type IN ('upsert', 'delete')),
        video_id INTEGER NOT NULL,
        chunk_id INTEGER,
        generation_id INTEGER,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'processing', 'completed', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at DATETIME,
        error_message TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # 12. Small durable state store for circuit breakers and restore/index fingerprints.
    """
    CREATE TABLE IF NOT EXISTS system_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # Indexes
    "CREATE INDEX IF NOT EXISTS idx_videos_parent_folder ON videos(parent_folder_id);",
    "CREATE INDEX IF NOT EXISTS idx_videos_md5 ON videos(md5_checksum);",
    "CREATE INDEX IF NOT EXISTS idx_query_cache_query ON query_cache(query);",
    "CREATE INDEX IF NOT EXISTS idx_videos_original_id ON videos(original_id);",
    "CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);",
    "CREATE INDEX IF NOT EXISTS idx_folders_parent_name ON folders(parent_id, name);",
    "CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(status, priority DESC, created_at ASC);",
    "CREATE INDEX IF NOT EXISTS idx_search_history_user ON search_history(user_id, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_index_outbox_pending ON index_outbox(status, next_attempt_at, id);",
    "CREATE INDEX IF NOT EXISTS idx_index_outbox_video ON index_outbox(video_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_index_generations_status ON index_generations(status, created_at);",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uidx_videos_md5_original
    ON videos(md5_checksum)
    WHERE original_id IS NULL AND md5_checksum IS NOT NULL AND md5_checksum != '';
    """,
    # Triggers
    """
    CREATE TRIGGER IF NOT EXISTS trg_videos_updated_at
    AFTER UPDATE ON videos
    BEGIN
        UPDATE videos SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_tasks_updated_at
    AFTER UPDATE ON tasks
    BEGIN
        UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_index_generations_updated_at
    AFTER UPDATE ON index_generations
    BEGIN
        UPDATE index_generations SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_index_outbox_updated_at
    AFTER UPDATE ON index_outbox
    BEGIN
        UPDATE index_outbox SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
    END;
    """,
    """
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
    """,
    """
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
    """,
    """
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
    """,
]


DB_MIGRATIONS = [
    # (table_name, column_name, alter_sql)
    ("tasks", "video_id", "ALTER TABLE tasks ADD COLUMN video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE"),
    ("tasks", "retries", "ALTER TABLE tasks ADD COLUMN retries INTEGER DEFAULT 0"),
    ("tasks", "max_retries", "ALTER TABLE tasks ADD COLUMN max_retries INTEGER DEFAULT 3"),
    ("videos", "is_silent", "ALTER TABLE videos ADD COLUMN is_silent BOOLEAN DEFAULT FALSE"),
    ("subtitle_flags", "locked_by", "ALTER TABLE subtitle_flags ADD COLUMN locked_by TEXT"),
    ("subtitle_flags", "locked_at", "ALTER TABLE subtitle_flags ADD COLUMN locked_at DATETIME"),
    ("chunks", "logical_id", "ALTER TABLE chunks ADD COLUMN logical_id TEXT"),
    ("chunks", "content_hash", "ALTER TABLE chunks ADD COLUMN content_hash TEXT"),
    ("chunks", "chunking_version", "ALTER TABLE chunks ADD COLUMN chunking_version TEXT NOT NULL DEFAULT 'legacy'"),
    ("chunks", "generation_id", "ALTER TABLE chunks ADD COLUMN generation_id INTEGER"),
    ("tasks", "dedupe_key", "ALTER TABLE tasks ADD COLUMN dedupe_key TEXT"),
    ("tasks", "next_attempt_at", "ALTER TABLE tasks ADD COLUMN next_attempt_at DATETIME"),
    ("tasks", "failure_kind", "ALTER TABLE tasks ADD COLUMN failure_kind TEXT"),
    ("tasks", "generation_id", "ALTER TABLE tasks ADD COLUMN generation_id INTEGER"),
]


POST_MIGRATION_STATEMENTS = [
    "CREATE UNIQUE INDEX IF NOT EXISTS uidx_chunks_logical_id ON chunks(logical_id) WHERE logical_id IS NOT NULL",
    "DROP INDEX IF EXISTS uidx_tasks_dedupe_key",
    """
    CREATE UNIQUE INDEX uidx_tasks_dedupe_key ON tasks(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running')
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_retry ON tasks(status, next_attempt_at, priority DESC, created_at)",
]
