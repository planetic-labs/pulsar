from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_app_settings, get_sqlite_settings
from app.db import db_connection

logger = logging.getLogger("app.services.task_queue")


@dataclass
class Task:
    """Модель задачи из очереди."""

    id: int
    task_type: str
    payload: dict[str, Any]
    video_id: int | None = None


class TaskQueueService:
    """Сервис для управления очередью фоновых задач."""

    def __init__(self) -> None:
        self.db_settings = get_sqlite_settings()

    async def claim_next(self, stage_types: list[str]) -> Task | None:
        """Атомарно выбирает и помечает следующую задачу к выполнению."""
        placeholders = ",".join(["?"] * len(stage_types))
        sql = f"""
            UPDATE tasks
            SET status = 'running', updated_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM tasks
                WHERE status = 'pending' AND task_type IN ({placeholders})
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
            )
            RETURNING id, task_type, payload, video_id;
        """
        try:
            with db_connection(self.db_settings) as conn:
                row = conn.execute(sql, stage_types).fetchone()
                if row:
                    payload = json.loads(row["payload"])
                    return Task(
                        id=row["id"],
                        task_type=row["task_type"],
                        payload=payload,
                        video_id=row["video_id"],
                    )
        except Exception as e:
            logger.error(f"Ошибка при выборе задачи из очереди ({stage_types}): {e}")
        return None

    async def advance_task(
        self, task_id: int, next_stage: str, payload: dict[str, Any], video_id: int | None = None
    ) -> None:
        """Переводит задачу на следующую стадию конвейера."""
        sql = """
            UPDATE tasks
            SET task_type = ?, payload = ?, status = 'pending', video_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """
        with db_connection(self.db_settings) as conn:
            conn.execute(sql, (next_stage, json.dumps(payload, ensure_ascii=False), video_id, task_id))

    async def complete_task(
        self,
        task_id: int,
        video_id: int | None = None,
        status: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Помечает задачу как успешно выполненную (или пропущенную)."""
        if payload is not None:
            sql = """
                UPDATE tasks
                SET status = ?, video_id = ?, payload = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            params = (status, video_id, json.dumps(payload, ensure_ascii=False), task_id)
        else:
            sql = """
                UPDATE tasks
                SET status = ?, video_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            params = (status, video_id, task_id)

        with db_connection(self.db_settings) as conn:
            conn.execute(sql, params)

    async def fail_task(self, task_id: int, error_trace: str) -> None:
        """Обрабатывает сбой задачи с учетом лимита повторных попыток."""
        with db_connection(self.db_settings) as conn:
            row = conn.execute("SELECT retries, max_retries, task_type FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return

            retries = row["retries"] or 0
            max_retries = row["max_retries"] or 3
            task_type = row["task_type"]

            new_retries = retries + 1
            if new_retries < max_retries:
                logger.warning(
                    f"Задача {task_id} ({task_type}) завершилась с ошибкой. Попытка {new_retries}/{max_retries}. "
                    f"Запланирован повторный запуск."
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'pending', retries = ?, error_message = ?,
                        created_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (new_retries, f"Попытка {new_retries} не удалась:\n{error_trace}", task_id),
                )
            else:
                logger.error(
                    f"Задача {task_id} ({task_type}) превысила лимит попыток ({max_retries}). "
                    "Переведена в статус failed."
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed', retries = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (new_retries, error_trace, task_id),
                )

    async def has_pending(self) -> bool:
        """Проверяет наличие задач в состоянии ожидания или выполнения."""
        sql = "SELECT COUNT(*) as c FROM tasks WHERE status IN ('pending', 'running')"
        try:
            with db_connection(self.db_settings) as conn:
                row = conn.execute(sql).fetchone()
                return row["c"] > 0
        except Exception as e:
            logger.error(f"Ошибка при проверке очереди задач: {e}")
            return True

    def cleanup(self) -> None:
        """Сбрасывает зависшие задачи в pending и удаляет устаревшие временные файлы."""
        app_settings = get_app_settings()
        active_audio_paths = set()

        with db_connection(self.db_settings) as conn:
            # 1. Сброс задач, зависших в состоянии 'running'
            conn.execute("UPDATE tasks SET status = 'pending' WHERE status = 'running'")

            # 2. Сбор путей аудиофайлов, которые нужны для незавершенных задач
            sql = "SELECT payload FROM tasks WHERE task_type = 'stage_2_transcribe' AND status IN ('pending', 'failed')"
            rows = conn.execute(sql).fetchall()
            for r in rows:
                try:
                    p = json.loads(r["payload"]).get("audio_path")
                    if p:
                        active_audio_paths.add(Path(p).resolve())
                except Exception:
                    continue

        # 3. Очистка временных файлов видео в downloads
        if app_settings.downloads_dir.exists():
            for p in app_settings.downloads_dir.glob("*"):
                if p.is_file():
                    try:
                        p.unlink()
                    except Exception:
                        pass

        # 4. Очистка неиспользуемых аудиофайлов в audio
        if app_settings.audio_dir.exists():
            for ext in ("*.wav", "*.ogg"):
                for p in app_settings.audio_dir.glob(ext):
                    if p.resolve() not in active_audio_paths:
                        try:
                            p.unlink()
                        except Exception:
                            pass

        logger.info("Очистка временных файлов и базы данных завершена.")
