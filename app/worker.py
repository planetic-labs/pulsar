import asyncio
import json
import logging
import traceback
from typing import Any

from qdrant_client import models

from app.config import get_embedding_settings, get_qdrant_settings, get_sqlite_settings
from app.db import db_connection
from app.gemini import UnifiedEmbeddingClient
from app.qdrant import get_qdrant_client
from app.repository import update_video_status
from scripts.ingest_drive_file import download_and_extract_stage, transcribe_stage

# BROADCAST LOGS TO WEBSOCKET
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
            _main_loop.call_soon_threadsafe(logs_queue.put_nowait, log_entry)


logger = logging.getLogger("app.worker")
ws_handler = WebSocketHandler()
ws_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%H:%M:%S"))
logger.addHandler(ws_handler)
logger.setLevel(logging.INFO)

# Capture all relevant loggers
for logger_name in ["scripts.ingest_drive_file", "app.worker", "app.voice"]:
    target_logger = logging.getLogger(logger_name)
    target_logger.addHandler(ws_handler)
    target_logger.setLevel(logging.INFO)


class Worker:
    def __init__(self, concurrency: int = 10):
        self.max_concurrency = concurrency
        self.semaphore = asyncio.Semaphore(concurrency)

        # RESOURCE LIMITERS (1 at a time for each type)
        self.download_sem = asyncio.Semaphore(1)
        self.transcribe_sem = asyncio.Semaphore(1)
        self.embed_sem = asyncio.Semaphore(1)

        self.is_running = False
        self._active_task_ids: set[int] = set()

    async def _run_stage_1_download(self, task_id: int, payload: dict):
        """Stage 1: Drive -> Audio (Local) -> Del Video."""
        async with self.download_sem:
            file_id = payload["file_id"]
            logger.info(f"--- [ЭТАП 1] Запуск загрузки: {file_id}")

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: download_and_extract_stage(file_id, status_callback=logger.info)
            )

            # Transition to Stage 2
            new_payload = {**payload, **result}
            sql = """
                UPDATE tasks
                SET task_type = 'stage_2_transcribe', payload = ?, status = 'pending', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            with db_connection(get_sqlite_settings()) as conn:
                conn.execute(sql, (json.dumps(new_payload), task_id))
            logger.info(f"--- [ЭТАП 1 ГОТОВО] Файл {file_id} подготовлен.")

    async def _run_stage_2_transcribe(self, task_id: int, payload: dict):
        """Stage 2: Audio -> Deepgram -> SQLite -> Del Audio."""
        async with self.transcribe_sem:
            file_id = payload["file_id"]
            audio_path = payload["audio_path"]
            logger.info(f"--- [ЭТАП 2] Запуск транскрибации: {payload['title']}")

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: transcribe_stage(file_id, audio_path, payload, status_callback=logger.info)
            )

            # Transition to Stage 3
            new_payload = {"video_id": result["video_id"]}
            sql = """
                UPDATE tasks
                SET task_type = 'stage_3_index', payload = ?, status = 'pending', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            with db_connection(get_sqlite_settings()) as conn:
                conn.execute(sql, (json.dumps(new_payload), task_id))
            logger.info(f"--- [ЭТАП 2 ГОТОВО] Текст для {payload['title']} сохранен.")

    async def _run_stage_3_index(self, task_id: int, payload: dict):
        """Stage 3: AI Service -> Qdrant."""
        async with self.embed_sem:
            video_id = payload["video_id"]
            logger.info(f"--- [ЭТАП 3] Запуск индексации (AI): {video_id}")

            settings = get_sqlite_settings()
            emb_settings = get_embedding_settings()
            q_settings = get_qdrant_settings()
            embed_client = UnifiedEmbeddingClient(emb_settings)
            qdrant = get_qdrant_client()

            with db_connection(settings) as conn:
                update_video_status(conn, video_id=video_id, processing_status="indexing")
                v_row = conn.execute(
                    "SELECT title, source_file_id, source_url FROM videos WHERE id = ?", (video_id,)
                ).fetchone()
                chunks = conn.execute(
                    "SELECT id, transcript_id, chunk_index, text FROM chunks WHERE video_id = ?", (video_id,)
                ).fetchall()

            texts = [c["text"] for c in chunks]
            loop = asyncio.get_running_loop()
            embeddings_data = await loop.run_in_executor(None, lambda: embed_client.embed_batch(texts))

            points = []
            for idx, row in enumerate(chunks):
                dense_vec, sparse_vec = embeddings_data[idx]
                vector_data: dict[str, Any] = {"default": dense_vec}
                if sparse_vec:
                    vector_data["text-sparse"] = sparse_vec

                points.append(
                    models.PointStruct(
                        id=row["id"],
                        vector=vector_data,
                        payload={
                            **row,
                            "video_id": video_id,
                            "title": v_row["title"],
                            "source_file_id": v_row["source_file_id"],
                            "is_primary": True,
                        },
                    )
                )

            if points:
                await loop.run_in_executor(
                    None, lambda: qdrant.upsert(collection_name=q_settings.collection_name, points=points)
                )

            with db_connection(settings) as conn:
                update_video_status(conn, video_id=video_id, processing_status="indexed_chunks_ready")
                conn.execute(
                    "UPDATE tasks SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (task_id,)
                )

            logger.info(f"--- [КОНВЕЙЕР ЗАВЕРШЕН] Видео {v_row['title']} полностью готово.")

    async def process_task(self, task_id: int, task_type: str, payload: dict):
        async with self.semaphore:
            self._active_task_ids.add(task_id)
            try:
                if task_type == "ingest_video":
                    task_type = "stage_1_download"

                if task_type == "stage_1_download":
                    await self._run_stage_1_download(task_id, payload)
                elif task_type == "stage_2_transcribe":
                    await self._run_stage_2_transcribe(task_id, payload)
                elif task_type == "stage_3_index":
                    await self._run_stage_3_index(task_id, payload)
                else:
                    logger.warning(f"Неизвестный тип задачи: {task_type}")
                    with db_connection(get_sqlite_settings()) as conn:
                        conn.execute(
                            "UPDATE tasks SET status = 'failed', error_message = 'Unknown task type' WHERE id = ?",
                            (task_id,),
                        )
            except Exception:
                error_trace = traceback.format_exc()
                logger.error(f"Ошибка в задаче {task_id}: {error_trace}")
                sql = (
                    "UPDATE tasks SET status = 'failed', error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                )
                with db_connection(get_sqlite_settings()) as conn:
                    conn.execute(sql, (error_trace, task_id))
            finally:
                self._active_task_ids.discard(task_id)

    def cleanup(self):
        settings = get_sqlite_settings()
        with db_connection(settings) as conn:
            conn.execute("UPDATE tasks SET status = 'pending' WHERE status = 'running'")
            conn.commit()

    async def run(self):
        self.cleanup()
        self.is_running = True
        logger.info("Воркер запущен: ТРЕХСТАДИЙНЫЙ КОНВЕЙЕР (Download -> Transcribe -> Index)")

        while self.is_running:
            try:
                slots = self.max_concurrency - len(self._active_task_ids)
                if slots > 0:
                    sql = """
                        SELECT id, task_type, payload FROM tasks
                        WHERE status = 'pending' ORDER BY priority DESC, created_at ASC LIMIT ?
                    """
                    with db_connection(get_sqlite_settings()) as conn:
                        rows = conn.execute(sql, (slots,)).fetchall()
                    for r in rows:
                        tid, ttype, tpayload = r["id"], r["task_type"], json.loads(r["payload"])
                        sql_upd = "UPDATE tasks SET status = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                        with db_connection(get_sqlite_settings()) as conn:
                            conn.execute(sql_upd, (tid,))
                        asyncio.create_task(self.process_task(tid, ttype, tpayload))
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Worker Loop Error: {e}")
                await asyncio.sleep(5)


_worker_instance = Worker(concurrency=10)


def get_worker():
    return _worker_instance
