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


# --- LOG BROADCASTING SYSTEM ---
class LogBroadcaster:
    def __init__(self):
        self.queues: list[asyncio.Queue[str]] = []

    def register(self) -> asyncio.Queue[str]:
        q = asyncio.Queue()
        self.queues.append(q)
        return q

    def unregister(self, q: asyncio.Queue[str]):
        if q in self.queues:
            self.queues.remove(q)

    def broadcast(self, message: str):
        global _main_loop
        if _main_loop:
            _main_loop.call_soon_threadsafe(self._do_broadcast, message)

    def _do_broadcast(self, message: str):
        for q in self.queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass


broadcaster = LogBroadcaster()
_main_loop = None


def set_main_loop(loop):
    global _main_loop
    _main_loop = loop


class WebSocketHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        broadcaster.broadcast(log_entry)


logger = logging.getLogger("app.worker")
ws_handler = WebSocketHandler()
ws_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%H:%M:%S"))
logger.addHandler(ws_handler)
logger.setLevel(logging.INFO)

log_names = ["scripts.ingest_drive_file", "app.worker", "app.voice", "app.gemini", "app.transcription.deepgram"]
for name in log_names:
    l_obj = logging.getLogger(name)
    l_obj.addHandler(ws_handler)
    l_obj.setLevel(logging.INFO)

# --- WORKER LOGIC ---


class Worker:
    def __init__(self, concurrency: int = 50):
        self.max_concurrency = concurrency
        self.download_sem = asyncio.Semaphore(1)
        self.transcribe_sem = asyncio.Semaphore(1)
        self.embed_sem = asyncio.Semaphore(1)
        self.is_running = False
        self._active_task_ids: set[int] = set()

    async def _run_stage_1_download(self, task_id: int, payload: dict):
        async with self.download_sem:
            file_id = payload["file_id"]

            sql_q = """
                SELECT COUNT(*) as c FROM tasks
                WHERE task_type IN ('stage_1_download', 'ingest_video')
                AND status IN ('pending', 'running')
                AND id != ?
            """
            with db_connection(get_sqlite_settings()) as conn:
                c_row = conn.execute(sql_q, (task_id,)).fetchone()
                in_queue = c_row["c"]

            logger.info(f"--- [ЭТАП 1] Загрузка: {file_id[:8]}... (В очереди: {in_queue})")
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: download_and_extract_stage(file_id, status_callback=logger.info)
            )

            new_payload = {**payload, **result}
            sql = """
                UPDATE tasks
                SET task_type = 'stage_2_transcribe', payload = ?, status = 'pending', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            with db_connection(get_sqlite_settings()) as conn:
                conn.execute(sql, (json.dumps(new_payload), task_id))
            logger.info(f"--- [ЭТАП 1 ГОТОВО] {result.get('title')} подготовлен.")

    async def _run_stage_2_transcribe(self, task_id: int, payload: dict):
        async with self.transcribe_sem:
            file_id = payload["file_id"]
            audio_path = payload["audio_path"]
            title = payload.get("title", file_id)
            logger.info(f"--- [ЭТАП 2] Транскрибация: {title}")
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: transcribe_stage(file_id, audio_path, payload, status_callback=logger.info)
            )

            new_payload = {"video_id": result["video_id"], "title": title}
            sql = """
                UPDATE tasks
                SET task_type = 'stage_3_index', payload = ?, status = 'pending', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
            with db_connection(get_sqlite_settings()) as conn:
                conn.execute(sql, (json.dumps(new_payload), task_id))
            logger.info(f"--- [ЭТАП 2 ГОТОВО] Текст для {title} сохранен.")

    async def _run_stage_3_index(self, task_id: int, payload: dict):
        async with self.embed_sem:
            video_id = payload["video_id"]
            title = payload.get("title", f"Video {video_id}")
            logger.info(f"--- [ЭТАП 3] Индексация: {title}")

            settings, q_settings = get_sqlite_settings(), get_qdrant_settings()
            embed_client = UnifiedEmbeddingClient(get_embedding_settings())
            qdrant = get_qdrant_client()

            with db_connection(settings) as conn:
                update_video_status(conn, video_id=video_id, processing_status="indexing")
                sql_v = "SELECT title, source_file_id, source_url FROM videos WHERE id = ?"
                v_row = conn.execute(sql_v, (video_id,)).fetchone()
                sql_c = """
                    SELECT id, transcript_id, chunk_index, text FROM chunks
                    WHERE video_id = ? ORDER BY chunk_index ASC
                """
                chunks = conn.execute(sql_c, (video_id,)).fetchall()

            if not chunks:
                with db_connection(settings) as conn:
                    update_video_status(conn, video_id=video_id, processing_status="indexed_chunks_ready")
                    conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,))
                return

            texts = [c["text"] for c in chunks]
            loop = asyncio.get_running_loop()
            embeddings_data = await loop.run_in_executor(None, lambda: embed_client.embed_batch(texts))

            points = []
            for idx, row in enumerate(chunks):
                dense_vec, sparse_vec = embeddings_data[idx]
                vd: dict[str, Any] = {"default": dense_vec}
                if sparse_vec:
                    vd["text-sparse"] = sparse_vec
                points.append(
                    models.PointStruct(
                        id=row["id"],
                        vector=vd,
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
                sql_f = "UPDATE tasks SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                conn.execute(sql_f, (task_id,))
            logger.info(f"=== [ГОТОВО] {v_row['title']} доступен для поиска. ===")

    async def process_task(self, task_id: int, task_type: str, payload: dict):
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
        except Exception:
            error_trace = traceback.format_exc()
            logger.error(f"Ошибка в задаче {task_id}: {error_trace}")
            sql = "UPDATE tasks SET status = 'failed', error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            with db_connection(get_sqlite_settings()) as conn:
                conn.execute(sql, (error_trace, task_id))
        finally:
            self._active_task_ids.discard(task_id)

    def cleanup(self):
        with db_connection(get_sqlite_settings()) as conn:
            conn.execute("UPDATE tasks SET status = 'pending' WHERE status = 'running'")

    async def run(self):
        self.cleanup()
        self.is_running = True
        logger.info("Воркер активен: ПАРАЛЛЕЛЬНЫЙ КОНВЕЙЕР")
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


_worker_instance = Worker(concurrency=50)


def get_worker():
    return _worker_instance
