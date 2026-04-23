import asyncio
import json
import logging
import traceback
from datetime import datetime

from app.config import get_sqlite_settings
from app.db import db_connection
from scripts.ingest_drive_file import ingest_drive_file

# Create a custom logging handler to broadcast logs to WebSocket
logs_queue: asyncio.Queue[str] = asyncio.Queue()
_main_loop = None


def set_main_loop(loop):
    global _main_loop
    _main_loop = loop


class WebSocketHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        global _main_loop
        if _main_loop:
            # Safely put message into the queue from any thread
            _main_loop.call_soon_threadsafe(logs_queue.put_nowait, log_entry)


logger = logging.getLogger("app.worker")
ws_handler = WebSocketHandler()
ws_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%H:%M:%S"))
logger.addHandler(ws_handler)
logger.setLevel(logging.INFO)

# Also capture logs from the ingestion script
logging.getLogger("scripts.ingest_drive_file").addHandler(ws_handler)
logging.getLogger("scripts.ingest_drive_file").setLevel(logging.INFO)


class Worker:
    def __init__(self, concurrency: int = 1):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.is_running = False

    async def process_task(self, task_id: int, task_type: str, payload: dict):
        async with self.semaphore:
            settings = get_sqlite_settings()

            with db_connection(settings) as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (task_id,)
                )

            try:
                if task_type == "ingest_video":
                    file_id = payload.get("file_id")
                    if not isinstance(file_id, str):
                        raise ValueError(f"Invalid file_id in task {task_id}: {file_id}")

                    logger.info(f">>> Начинаю обработку файла {file_id}")

                    def progress_callback(downloaded: int, total: int):
                        percent = (downloaded / total) * 100
                        # Log every few percent to keep UI alive but not flooded
                        if int(percent * 10) % 50 == 0:
                            logger.info(f"Прогресс скачивания: {percent:.1f}%")

                    loop = asyncio.get_running_loop()
                    diarize = bool(payload.get("diarize", True))

                    # Wrapper to ensure types are seen correctly by checkers
                    def run_ingest():
                        return ingest_drive_file(
                            file_id=str(file_id), diarize=diarize, download_progress_callback=progress_callback
                        )

                    result = await loop.run_in_executor(None, run_ingest)

                    logger.info(f"Успех! Видео {result['video_id']} полностью проиндексировано.")

                    with db_connection(settings) as conn:
                        conn.execute(
                            "UPDATE tasks SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (task_id,),
                        )
                else:
                    raise ValueError(f"Unknown task type: {task_type}")

            except Exception as e:
                error_trace = traceback.format_exc()
                logger.error(f"Ошибка в задаче {task_id}: {str(e)}")
                with db_connection(settings) as conn:
                    conn.execute(
                        """
                        UPDATE tasks SET status = 'failed', error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (error_trace, task_id),
                    )

    async def run(self):
        self.is_running = True
        logger.info("Воркер запущен. Ожидание задач в очереди...")

        # Keepalive ping task
        async def keepalive():
            while self.is_running:
                await asyncio.sleep(10)
                if logs_queue.empty():
                    await logs_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] INFO: Воркер в режиме ожидания...")

        asyncio.create_task(keepalive())

        settings = get_sqlite_settings()
        while self.is_running:
            try:
                with db_connection(settings) as conn:
                    row = conn.execute(
                        """
                        SELECT id, task_type, payload FROM tasks
                        WHERE status = 'pending' ORDER BY priority DESC, created_at ASC LIMIT 1
                        """
                    ).fetchone()

                if row:
                    await self.process_task(row["id"], row["task_type"], json.loads(row["payload"]))
                else:
                    await asyncio.sleep(3)
            except Exception as e:
                logging.getLogger(__name__).error(f"Worker loop error: {e}")
                await asyncio.sleep(5)


_worker_instance = Worker(concurrency=1)


def get_worker():
    return _worker_instance
