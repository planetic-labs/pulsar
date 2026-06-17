from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Callable
from typing import Any

from app.config import get_sqlite_settings
from app.db import db_connection
from app.pipeline.download import InsufficientSpaceError
from app.services.ingest import IngestService
from app.services.task_queue import TaskQueueService


# --- LOG BROADCASTING SYSTEM ---
class LogBroadcaster:
    def __init__(self) -> None:
        self.queues: list[asyncio.Queue[str]] = []

    def register(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self.queues.append(q)
        return q

    def unregister(self, q: asyncio.Queue[str]) -> None:
        if q in self.queues:
            self.queues.remove(q)

    def broadcast(self, message: str) -> None:
        global _main_loop
        if _main_loop and _main_loop.is_running():
            _main_loop.call_soon_threadsafe(self._do_broadcast, message)

    def _do_broadcast(self, message: str) -> None:
        for q in self.queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass


broadcaster = LogBroadcaster()
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


class WebSocketHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        log_entry = self.format(record)
        broadcaster.broadcast(log_entry)


logger = logging.getLogger("app.worker")
ws_handler = WebSocketHandler()
ws_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%H:%M:%S"))
logger.addHandler(ws_handler)
logger.setLevel(logging.INFO)

log_names = [
    "scripts.ingest_drive_file",
    "app.worker",
    "app.voice",
    "app.embeddings",
    "app.transcription.deepgram",
    "app.audio",
    "app.pipeline.download",
    "app.pipeline.transcribe",
    "app.pipeline.index",
    "app.services.ingest",
    "app.services.task_queue",
]
for name in log_names:
    l_obj = logging.getLogger(name)
    l_obj.addHandler(ws_handler)
    l_obj.setLevel(logging.INFO)


class Worker:
    """Параллельный воркер обработки видео на основе стадий конвейера (Pipeline)."""

    def __init__(self) -> None:
        self.queue = TaskQueueService()
        self.ingest = IngestService()
        self.is_running = False
        self.is_stopping = False

        # Ограничения конкурентности для каждой стадии
        self.semaphores = {
            "stage_1_download": asyncio.Semaphore(1),
            "ingest_video": asyncio.Semaphore(1),
            "stage_2_transcribe": asyncio.Semaphore(1),
            "stage_3_index": asyncio.Semaphore(1),
        }

        self._state = {
            "stage_1_download": {"active": False, "title": "", "progress": 0, "speed": "", "status_text": "Ожидание"},
            "stage_2_transcribe": {"active": False, "title": "", "progress": 0, "speed": "", "status_text": "Ожидание"},
            "stage_3_index": {"active": False, "title": "", "progress": 0, "status_text": "Ожидание"},
        }

    def get_progress_state(self) -> dict[str, dict[str, Any]]:
        """Возвращает текущее состояние прогресса всех стадий (для UI/API)."""
        return self._state

    def stop(self) -> None:
        """Мягкая остановка воркера."""
        if self.is_running and not self.is_stopping:
            logger.info("Запрос на остановку воркера. Завершение текущих задач...")
            self.is_running = False
            self.is_stopping = True

    def _get_stage_key(self, stage_type: str) -> str:
        if stage_type == "ingest_video":
            return "stage_1_download"
        return stage_type

    def _make_progress_callback(self, stage_key: str) -> Callable[[dict[str, Any]], None]:
        def cb(data: dict[str, Any]) -> None:
            self._state[stage_key].update(data)

        return cb

    async def _run_task(self, task_id: int, task_type: str, payload: dict[str, Any]) -> None:
        stage_key = self._get_stage_key(task_type)
        progress_cb = self._make_progress_callback(stage_key)
        sem = self.semaphores.get(task_type)

        if not sem:
            sem = asyncio.Semaphore(1)

        async with sem:
            try:
                result = await self.ingest.execute_stage(task_type, task_id, payload, progress_cb)

                if result.success:
                    if result.status == "skipped_duplicate_md5":
                        video_id = result.next_payload.get("video_id") if result.next_payload else None
                        await self.queue.complete_task(task_id, video_id=video_id, status="skipped_duplicate_md5")
                    elif result.status == "completed_silent":
                        video_id = result.next_payload.get("video_id") if result.next_payload else None
                        await self.queue.complete_task(task_id, video_id=video_id, status="completed")
                    else:
                        next_payload = result.next_payload or {}
                        # Определяем следующую стадию
                        if task_type in ("stage_1_download", "ingest_video"):
                            await self.queue.advance_task(task_id, "stage_2_transcribe", next_payload)
                        elif task_type == "stage_2_transcribe":
                            video_id = next_payload.get("video_id")
                            index_payload = {"video_id": video_id, "title": next_payload.get("title")}
                            await self.queue.advance_task(task_id, "stage_3_index", index_payload, video_id=video_id)
                        elif task_type == "stage_3_index":
                            await self.queue.complete_task(task_id, video_id=payload.get("video_id"))
                else:
                    await self.queue.fail_task(task_id, result.error or "Unknown error")

            except InsufficientSpaceError as e:
                title = payload.get("title", f"File {payload.get('file_id')}")
                logger.warning(f"Недостаточно места для {title}: {e}")

                # Проверяем, есть ли другие задачи в транскрибации
                sql_check = """
                    SELECT COUNT(*) as c FROM tasks
                    WHERE task_type = 'stage_2_transcribe' AND status IN ('pending', 'running')
                """
                with db_connection(get_sqlite_settings()) as conn:
                    t_count = conn.execute(sql_check).fetchone()["c"]

                if t_count > 0:
                    # Возвращаем задачу в очередь и ждем
                    await self.queue.complete_task(task_id, status="pending")
                    logger.info("Есть задачи на транскрибацию. Ждем 60 секунд...")
                    await asyncio.sleep(60)
                else:
                    # Пропускаем задачу из-за нехватки места
                    new_payload = {**payload, "file_size": e.file_size}
                    await self.queue.complete_task(task_id, status="skipped_no_space", payload=new_payload)
                    logger.error(f"Недостаточно места. Файл {title} пропущен.")

            except FileNotFoundError as e:
                # Если во время транскрибации пропал аудиофайл
                title = payload.get("title", "Unknown")
                file_id = payload.get("file_id")
                logger.warning(
                    f"Аудиофайл для {title} не найден на диске. "
                    f"Создаем новую задачу на скачивание и удаляем текущую задачу транскрибации. Ошибка: {e}"
                )
                with db_connection(get_sqlite_settings()) as conn:
                    conn.execute(
                        "INSERT INTO tasks (task_type, payload, status) VALUES ('stage_1_download', ?, 'pending')",
                        (json.dumps({"file_id": file_id, "title": title}),),
                    )
                    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

            except Exception:
                error_trace = traceback.format_exc()
                logger.error(f"Ошибка в задаче {task_id} ({task_type}): {error_trace}")
                await self.queue.fail_task(task_id, error_trace)

            finally:
                self._state[stage_key].update(
                    {"active": False, "title": "", "progress": 0, "speed": "", "status_text": "Ожидание"}
                )

    async def _consume_stage(self, stage_types: list[str]) -> None:
        """Бесконечный цикл обработки задач определенного типа."""
        while self.is_running:
            try:
                task = await self.queue.claim_next(stage_types)
                if task:
                    await self._run_task(task.id, task.task_type, task.payload)
                else:
                    # Если задач нет в этой группе очередей, проверяем, есть ли они вообще в системе
                    if not await self.queue.has_pending():
                        logger.info("Очередь пуста. Автоматическая остановка воркера для экономии ресурсов.")
                        self.is_running = False
                        break
                    await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Ошибка в консьюмере {stage_types}: {e}")
                await asyncio.sleep(5)

    async def run(self) -> None:
        if self.is_running:
            logger.warning("Воркер уже запущен.")
            return

        self.queue.cleanup()
        self.is_running = True
        self.is_stopping = False
        logger.info("Воркер активен: ТРЕХСТАДИЙНЫЙ ПАРАЛЛЕЛЬНЫЙ КОНВЕЙЕР (PIPELINE)")

        try:
            await asyncio.gather(
                self._consume_stage(["stage_1_download", "ingest_video"]),
                self._consume_stage(["stage_2_transcribe"]),
                self._consume_stage(["stage_3_index"]),
            )
        finally:
            self.is_running = False
            self.is_stopping = False
            logger.info("Воркер остановлен.")


_worker_instance = Worker()


def get_worker() -> Worker:
    return _worker_instance
