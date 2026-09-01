import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import get_app_settings, get_sqlite_settings
from app.db import db_connection
from app.indexing_state import (
    embedding_circuit_is_open,
    ensure_active_generation,
    index_task_dedupe_key,
    is_permanent_provider_error,
    open_embedding_circuit,
)

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
                  AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP)
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
            )
            RETURNING id, task_type, payload, video_id;
        """

        def _sync_claim() -> Any:
            with db_connection(self.db_settings) as conn:
                eligible_types = list(stage_types)
                if "stage_3_index" in eligible_types and embedding_circuit_is_open(conn):
                    eligible_types.remove("stage_3_index")
                if not eligible_types:
                    return None
                eligible_placeholders = ",".join(["?"] * len(eligible_types))
                eligible_sql = sql.replace(placeholders, eligible_placeholders)
                return conn.execute(eligible_sql, eligible_types).fetchone()

        try:
            row = await asyncio.to_thread(_sync_claim)
            if not row:
                return None

            task_id = row["id"]
            task_type = row["task_type"]
            raw_payload = row["payload"]
            video_id = row["video_id"]

            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError as je:
                je_str = str(je)
                logger.error(f"Битый payload в задаче {task_id}: {raw_payload!r}. Переводим в failed. Ошибка: {je_str}")

                def _sync_fail() -> None:
                    with db_connection(self.db_settings) as conn:
                        conn.execute(
                            "UPDATE tasks SET status = 'failed', error_message = ? WHERE id = ?",
                            (f"Битый JSON payload: {je_str}", task_id),
                        )

                await asyncio.to_thread(_sync_fail)
                return None

            return Task(
                id=task_id,
                task_type=task_type,
                payload=payload,
                video_id=video_id,
            )
        except Exception as e:
            logger.error(f"Ошибка при выборе задачи из очереди ({stage_types}): {e}")
            raise

    async def advance_task(
        self, task_id: int, next_stage: str, payload: dict[str, Any], video_id: int | None = None
    ) -> None:
        """Переводит задачу на следующую стадию конвейера."""

        def _sync() -> None:
            with db_connection(self.db_settings) as conn:
                generation_id = None
                dedupe_key = None
                resolved_payload = payload
                if next_stage == "stage_3_index" and video_id is not None:
                    generation_id = ensure_active_generation(conn)
                    dedupe_key = index_task_dedupe_key(conn, video_id, generation_id)
                    duplicate = conn.execute(
                        """
                        SELECT id FROM tasks
                        WHERE dedupe_key = ? AND id != ? AND status IN ('pending', 'running')
                        """,
                        (dedupe_key, task_id),
                    ).fetchone()
                    if duplicate:
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status = 'superseded', error_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (f"Superseded by idempotent task {duplicate['id']}", task_id),
                        )
                        return
                    resolved_payload = {**payload, "generation_id": generation_id}

                conn.execute(
                    """
                    UPDATE tasks
                    SET task_type = ?, payload = ?, status = 'pending', video_id = ?,
                        generation_id = ?, dedupe_key = ?, retries = 0,
                        failure_kind = NULL, error_message = NULL, next_attempt_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        next_stage,
                        json.dumps(resolved_payload, ensure_ascii=False, sort_keys=True),
                        video_id,
                        generation_id,
                        dedupe_key,
                        task_id,
                    ),
                )

        await asyncio.to_thread(_sync)

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
                SET status = ?, video_id = ?, payload = ?, error_message = NULL,
                    failure_kind = NULL, next_attempt_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            params = (status, video_id, json.dumps(payload, ensure_ascii=False), task_id)
        else:
            sql = """
                UPDATE tasks
                SET status = ?, video_id = ?, error_message = NULL,
                    failure_kind = NULL, next_attempt_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            params = (status, video_id, task_id)

        def _sync() -> None:
            with db_connection(self.db_settings) as conn:
                conn.execute(sql, params)

        await asyncio.to_thread(_sync)

    async def fail_task(self, task_id: int, error_trace: str, *, permanent: bool | None = None) -> None:
        """Classify a failure and retry transient errors with bounded exponential backoff."""

        def _sync() -> None:
            with db_connection(self.db_settings) as conn:
                row = conn.execute(
                    "SELECT retries, max_retries, task_type FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if not row:
                    return

                retries = row["retries"] or 0
                max_retries = row["max_retries"] or 3
                task_type = row["task_type"]

                resolved_permanent = permanent
                if resolved_permanent is None:
                    resolved_permanent = is_permanent_provider_error(error_trace)

                new_retries = retries + 1
                if resolved_permanent:
                    logger.error(
                        "Задача %s (%s) завершилась постоянной ошибкой и не будет повторена.",
                        task_id,
                        task_type,
                    )
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'failed', retries = ?, error_message = ?,
                            failure_kind = 'permanent', next_attempt_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (new_retries, error_trace, task_id),
                    )
                    if task_type == "stage_3_index":
                        open_embedding_circuit(conn, error_trace, permanent=True)
                elif new_retries < max_retries:
                    delay_seconds = min(15 * (2 ** (new_retries - 1)), 15 * 60)
                    next_attempt = datetime.now(UTC) + timedelta(seconds=delay_seconds)
                    logger.warning(
                        "Задача %s (%s) завершилась с ошибкой. Попытка %s/%s; повтор через %s с.",
                        task_id,
                        task_type,
                        new_retries,
                        max_retries,
                        delay_seconds,
                    )
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'pending', retries = ?, error_message = ?,
                            failure_kind = 'transient', next_attempt_at = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            new_retries,
                            f"Попытка {new_retries} не удалась:\n{error_trace}",
                            next_attempt.strftime("%Y-%m-%d %H:%M:%S"),
                            task_id,
                        ),
                    )
                else:
                    logger.error(
                        f"Задача {task_id} ({task_type}) превысила лимит попыток ({max_retries}). "
                        "Переведена в статус failed."
                    )
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'failed', retries = ?, error_message = ?,
                            failure_kind = 'retry_exhausted', next_attempt_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (new_retries, error_trace, task_id),
                    )

        await asyncio.to_thread(_sync)

    async def has_pending(self) -> bool:
        """Проверяет наличие задач в состоянии ожидания или выполнения."""
        sql = "SELECT COUNT(*) as c FROM tasks WHERE status IN ('pending', 'running')"

        def _sync() -> bool:
            with db_connection(self.db_settings) as conn:
                row = conn.execute(sql).fetchone()
                return row["c"] > 0

        try:
            return await asyncio.to_thread(_sync)
        except Exception as e:
            logger.error(f"Ошибка при проверке очереди задач: {e}")
            raise

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
                except json.JSONDecodeError, TypeError, ValueError:
                    continue

        # 3. Очистка временных файлов видео в downloads
        if app_settings.downloads_dir.exists():
            for p in app_settings.downloads_dir.glob("*"):
                if p.is_file():
                    with contextlib.suppress(OSError):
                        p.unlink()

        # 4. Очистка неиспользуемых аудиофайлов в audio
        if app_settings.audio_dir.exists():
            for ext in ("*.wav", "*.ogg"):
                for p in app_settings.audio_dir.glob(ext):
                    if p.resolve() not in active_audio_paths:
                        with contextlib.suppress(OSError):
                            p.unlink()

        logger.info("Очистка временных файлов и базы данных завершена.")
