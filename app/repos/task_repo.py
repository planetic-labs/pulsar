from __future__ import annotations

import json
from typing import Any

from app.database import Database


class TaskRepository:
    """Репозиторий для управления фоновыми задачами в БД."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_task(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int = 0,
        video_id: int | None = None,
    ) -> int:
        """Создает задачу в очереди."""
        sql = """
            INSERT INTO tasks (task_type, payload, priority, video_id, status)
            VALUES (?, ?, ?, ?, 'pending')
            RETURNING id
        """
        async with self.db.transaction() as conn:
            async with conn.execute(
                sql, (task_type, json.dumps(payload, ensure_ascii=False), priority, video_id)
            ) as cursor:
                row = await cursor.fetchone()
                assert row is not None
                return int(row["id"])

    async def get_by_id(self, task_id: int) -> dict[str, Any] | None:
        """Возвращает задачу по идентификатору."""
        async with self.db.transaction() as conn:
            async with conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def update_status(
        self,
        task_id: int,
        status: str,
        video_id: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Обновляет статус и результаты выполнения задачи."""
        sql = """
            UPDATE tasks
            SET status = ?, video_id = COALESCE(?, video_id), error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """
        async with self.db.transaction() as conn:
            await conn.execute(sql, (status, video_id, error_message, task_id))

    async def delete(self, task_id: int) -> None:
        """Удаляет задачу из очереди."""
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
