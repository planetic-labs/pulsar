from __future__ import annotations

from typing import Any

from app.database import Database


class FolderRepository:
    """Репозиторий для управления структурой папок."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(self, folder_id: str, name: str, parent_id: str | None = None) -> None:
        """Вставляет или обновляет папку."""
        sql = """
            INSERT INTO folders (id, name, parent_id)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = EXCLUDED.name,
                parent_id = EXCLUDED.parent_id
        """
        async with self.db.transaction() as conn:
            await conn.execute(sql, (folder_id, name, parent_id))

    async def get_by_id(self, folder_id: str) -> dict[str, Any] | None:
        """Возвращает папку по идентификатору."""
        async with self.db.transaction() as conn:
            async with conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
