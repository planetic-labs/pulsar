from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

logger = logging.getLogger("app.database")


def sqlite_regexp(expr: str, item: str | None) -> bool:
    """Реализация REGEXP для SQLite."""
    if item is None:
        return False
    try:
        return re.search(expr, item, re.IGNORECASE) is not None
    except Exception:
        return False


class Database:
    """Асинхронное подключение к SQLite с поддержкой транзакций."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Устанавливает соединение с базой данных и регистрирует PRAGMA."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path), timeout=30.0)
        self._conn.row_factory = aiosqlite.Row

        # Регистрация кастомных функций
        await self._conn.create_function("REGEXP", 2, sqlite_regexp)

        # Настройки производительности
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.execute("PRAGMA mmap_size=1073741824;")
        await self._conn.execute("PRAGMA cache_size=-102400;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Предоставляет транзакционный контекст."""
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        try:
            yield self._conn
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def close(self) -> None:
        """Закрывает соединение с базой данных."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def init_schema(self) -> None:
        """Инициализация схемы базы данных, индексов и триггеров."""
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")

        async with self.transaction() as conn:
            # 0. Folders table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS folders (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    parent_id TEXT REFERENCES folders(id) ON DELETE CASCADE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    CHECK (parent_id IS NULL OR parent_id != id)
                )
            """)

            # 1. Videos table
            await conn.execute("""
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
                )
            """)

            # Migrate: add is_silent column
            try:
                await conn.execute("ALTER TABLE videos ADD COLUMN is_silent BOOLEAN DEFAULT FALSE")
            except aiosqlite.OperationalError:
                pass

            # 3. Chunks table
            await conn.execute("""
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

            # 4. Tasks table
            await conn.execute("""
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

            # Migrations for tasks columns
            async with conn.execute("PRAGMA table_info(tasks)") as cursor:
                columns = [row[1] for row in await cursor.fetchall()]

            if "video_id" not in columns:
                try:
                    await conn.execute(
                        "ALTER TABLE tasks ADD COLUMN video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE"
                    )
                except Exception as e:
                    logger.error(f"Error adding video_id column to tasks: {e}")

            if "retries" not in columns:
                try:
                    await conn.execute("ALTER TABLE tasks ADD COLUMN retries INTEGER DEFAULT 0")
                except Exception as e:
                    logger.error(f"Error adding retries column to tasks: {e}")

            if "max_retries" not in columns:
                try:
                    await conn.execute("ALTER TABLE tasks ADD COLUMN max_retries INTEGER DEFAULT 3")
                except Exception as e:
                    logger.error(f"Error adding max_retries column to tasks: {e}")

            # 5. Query cache table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS query_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT UNIQUE NOT NULL,
                    dense_vector BLOB NOT NULL,
                    sparse_indices BLOB,
                    sparse_values BLOB,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 6. Indexes
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_parent_folder ON videos(parent_folder_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_md5 ON videos(md5_checksum)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_query_cache_query ON query_cache(query)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_original_id ON videos(original_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_folders_parent_name ON folders(parent_id, name)")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(status, priority DESC, created_at ASC)"
            )
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uidx_videos_md5_original
                ON videos(md5_checksum)
                WHERE original_id IS NULL AND md5_checksum IS NOT NULL AND md5_checksum != ''
            """)

            # 7. Triggers
            await conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_videos_updated_at
                AFTER UPDATE ON videos
                BEGIN
                    UPDATE videos SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
                END;
            """)
            await conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_tasks_updated_at
                AFTER UPDATE ON tasks
                BEGIN
                    UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
                END;
            """)
            await conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_videos_prevent_duplicate_chains_insert
                BEFORE INSERT ON videos
                BEGIN
                    SELECT CASE
                        WHEN NEW.original_id IS NOT NULL AND (
                            SELECT original_id FROM videos WHERE id = NEW.original_id
                        ) IS NOT NULL
                        THEN RAISE(
                            ABORT,
                            'Циклическая или многоуровневая копия: оригинальный файл сам является дубликатом!'
                        )
                    END;
                END;
            """)
            await conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_videos_prevent_duplicate_chains_update
                BEFORE UPDATE ON videos
                BEGIN
                    SELECT CASE
                        WHEN NEW.original_id IS NOT NULL AND (
                            SELECT original_id FROM videos WHERE id = NEW.original_id
                        ) IS NOT NULL
                        THEN RAISE(
                            ABORT,
                            'Циклическая или многоуровневая копия: оригинальный файл сам является дубликатом!'
                        )
                    END;
                END;
            """)
            await conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_folders_prevent_loops
                BEFORE UPDATE ON folders
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

            # 8. Revoked sessions table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS revoked_sessions (
                    jti TEXT PRIMARY KEY,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 9. Revoked users table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS revoked_users (
                    user_id TEXT PRIMARY KEY,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 10. Integrity issues table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS integrity_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
        logger.info("SQLite Database schema initialized via aiosqlite.")
