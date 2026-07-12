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

            from app.db_schema import DB_MIGRATIONS

            table_columns = {}
            for table_name, column_name, alter_sql in DB_MIGRATIONS:
                if table_name not in table_columns:
                    async with conn.execute(f"PRAGMA table_info({table_name})") as cursor:
                        table_columns[table_name] = [row[1] for row in await cursor.fetchall()]

                if column_name not in table_columns[table_name]:
                    try:
                        await conn.execute(alter_sql)
                        logger.info(f"Migration: Added column '{column_name}' to table '{table_name}'.")
                        table_columns[table_name].append(column_name)
                    except aiosqlite.Error as e:
                        logger.error(f"Migration failed: '{alter_sql}': {e}")

        logger.info("SQLite Database schema initialized via aiosqlite.")
