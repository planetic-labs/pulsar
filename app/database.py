from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.db_schema import PRAGMAS, SCHEMA_STATEMENTS, sqlite_regexp

logger = logging.getLogger("app.database")


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
        for pragma in PRAGMAS:
            await self._conn.execute(pragma)

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Предоставляет транзакционный контекст."""
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        try:
            yield self._conn
            await self._conn.commit()
        except BaseException:
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
            for stmt in SCHEMA_STATEMENTS:
                await conn.execute(stmt)

            # Migrations for tasks columns
            async with conn.execute("PRAGMA table_info(tasks)") as cursor:
                columns = [row[1] for row in await cursor.fetchall()]

            if "video_id" not in columns:
                try:
                    await conn.execute(
                        "ALTER TABLE tasks ADD COLUMN video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE"
                    )
                except aiosqlite.Error as e:
                    logger.error(f"Error adding video_id column to tasks: {e}")

            if "retries" not in columns:
                try:
                    await conn.execute("ALTER TABLE tasks ADD COLUMN retries INTEGER DEFAULT 0")
                except aiosqlite.Error as e:
                    logger.error(f"Error adding retries column to tasks: {e}")

            if "max_retries" not in columns:
                try:
                    await conn.execute("ALTER TABLE tasks ADD COLUMN max_retries INTEGER DEFAULT 3")
                except aiosqlite.Error as e:
                    logger.error(f"Error adding max_retries column to tasks: {e}")

            # Migrate: add is_silent column
            async with conn.execute("PRAGMA table_info(videos)") as cursor:
                v_columns = [row[1] for row in await cursor.fetchall()]
            if "is_silent" not in v_columns:
                try:
                    await conn.execute("ALTER TABLE videos ADD COLUMN is_silent BOOLEAN DEFAULT FALSE")
                except aiosqlite.OperationalError:
                    pass

        logger.info("SQLite Database schema initialized via aiosqlite.")
