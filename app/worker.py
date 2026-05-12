import asyncio
import json
import logging
import traceback
from pathlib import Path
from typing import Any

from qdrant_client import models

from app.audio import SilentVideoError
from app.config import (
    get_deepgram_settings,
    get_embedding_settings,
    get_qdrant_settings,
    get_sqlite_settings,
)
from app.db import db_connection
from app.gemini import UnifiedEmbeddingClient
from app.qdrant import get_qdrant_client
from app.repository import update_video_status
from app.transcription.deepgram import DeepgramEngine
from scripts.ingest_drive_file import (
    InsufficientSpaceError,
    download_and_extract_stage,
    transcribe_stage,
)


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

log_names = [
    "scripts.ingest_drive_file",
    "app.worker",
    "app.voice",
    "app.gemini",
    "app.transcription.deepgram",
    "app.audio",
]
for name in log_names:
    l_obj = logging.getLogger(name)
    l_obj.addHandler(ws_handler)
    l_obj.setLevel(logging.INFO)

# --- WORKER LOGIC ---


class Worker:
    def __init__(self):
        self.download_sem = asyncio.Semaphore(1)
        self.transcribe_sem = asyncio.Semaphore(1)
        self.embed_sem = asyncio.Semaphore(1)
        self.is_running = False
        self.is_stopping = False
        self._run_task: asyncio.Task | None = None
        self._state = {
            "stage_1_download": {"active": False, "title": "", "progress": 0, "speed": "", "status_text": "Ожидание"},
            "stage_2_transcribe": {"active": False, "title": "", "progress": 0, "speed": "", "status_text": "Ожидание"},
            "stage_3_index": {"active": False, "title": "", "progress": 0, "status_text": "Ожидание"},
        }

    def get_progress_state(self) -> dict:
        """Возвращает текущее состояние прогресса всех стадий."""
        return self._state

    def stop(self):
        """Остановка воркера."""
        if self.is_running and not self.is_stopping:
            logger.info("Запрос на остановку воркера. Завершение текущих задач...")
            self.is_running = False
            self.is_stopping = True

    async def _has_pending_tasks(self) -> bool:
        """Проверка наличия задач в очереди или в работе."""
        sql = "SELECT COUNT(*) as c FROM tasks WHERE status IN ('pending', 'running')"
        try:
            with db_connection(get_sqlite_settings()) as conn:
                row = conn.execute(sql).fetchone()
                return row["c"] > 0
        except Exception as e:
            logger.error(f"Ошибка при проверке очереди: {e}")
            return True  # В случае ошибки лучше считать, что задачи есть

    async def _run_stage_1_download(self, task_id: int, payload: dict):
        async with self.download_sem:
            file_id = payload["file_id"]
            title = payload.get("title", f"File {file_id}")

            self._state["stage_1_download"].update(
                {"active": True, "title": title, "progress": 0, "speed": "", "status_text": "Инициализация"}
            )

            sql_q = "SELECT COUNT(*) as c FROM tasks WHERE status IN ('pending', 'running')"
            with db_connection(get_sqlite_settings()) as conn:
                c_row = conn.execute(sql_q).fetchone()
                in_queue = c_row["c"]

            def update_state(data: dict):
                self._state["stage_1_download"].update(data)

            try:
                result = await download_and_extract_stage(
                    file_id, status_callback=logger.info, in_queue=in_queue, state_callback=update_state
                )

                # Use the MD5 from the initial ingest request if available,
                # otherwise use what was fetched during download
                md5 = payload.get("md5") or result.get("md5_checksum")
                new_payload = {**payload, **result, "md5_checksum": md5}
                sql = """
                    UPDATE tasks
                    SET task_type = 'stage_2_transcribe', payload = ?, status = 'pending',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """
                with db_connection(get_sqlite_settings()) as conn:
                    conn.execute(sql, (json.dumps(new_payload), task_id))
                logger.info(f"{result.get('title')} подготовлен.")
            except InsufficientSpaceError as e:
                logger.warning(f"Недостаточно места для {title}: {e}")

                # Check if there are tasks in transcription queue
                sql_check = (
                    "SELECT COUNT(*) as c FROM tasks "
                    "WHERE task_type = 'stage_2_transcribe' AND status IN ('pending', 'running')"
                )
                with db_connection(get_sqlite_settings()) as conn:
                    t_count = conn.execute(sql_check).fetchone()["c"]

                if t_count > 0:
                    # Re-queue and wait
                    sql_requeue = "UPDATE tasks SET status = 'pending', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                    with db_connection(get_sqlite_settings()) as conn:
                        conn.execute(sql_requeue, (task_id,))
                    logger.info("Есть задачи на транскрибацию. Ждем 60 секунд...")
                    await asyncio.sleep(60)
                else:
                    # Skip the file
                    new_payload = {**payload, "file_size": e.file_size}
                    sql_skip = (
                        "UPDATE tasks SET status = 'skipped_no_space', payload = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                    )
                    with db_connection(get_sqlite_settings()) as conn:
                        conn.execute(sql_skip, (json.dumps(new_payload), task_id))
                    logger.error(f"Недостаточно места. Файл {title} пропущен.")
            except SilentVideoError:
                logger.warning(f"Пропуск видео без звука: {title}")
                sql = "UPDATE tasks SET status = 'skipped_silent', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                with db_connection(get_sqlite_settings()) as conn:
                    conn.execute(sql, (task_id,))
            except Exception as e:
                logger.error(f"Ошибка в задаче {task_id} (stage_1_download): {traceback.format_exc()}")
                sql = (
                    "UPDATE tasks SET status = 'failed', error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                )
                with db_connection(get_sqlite_settings()) as conn:
                    conn.execute(sql, (str(e), task_id))
            finally:
                self._state["stage_1_download"].update(
                    {"active": False, "title": "", "progress": 0, "speed": "", "status_text": "Ожидание"}
                )

    async def _run_stage_2_transcribe(self, task_id: int, payload: dict):
        async with self.transcribe_sem:
            # Check Deepgram Balance before starting
            dg_settings = get_deepgram_settings()
            engine = DeepgramEngine(dg_settings)
            is_ok, amount = await engine.check_balance_threshold_async(1.0)

            if not is_ok:
                err_msg = f"Отказ в транскрибации: баланс Deepgram (${amount:.2f}) ниже порога $1.00"
                logger.error(err_msg)
                raise RuntimeError(err_msg)

            file_id = payload["file_id"]
            audio_path = payload["audio_path"]
            title = payload.get("title", file_id)

            self._state["stage_2_transcribe"].update(
                {"active": True, "title": title, "progress": 0, "speed": "", "status_text": "Инициализация"}
            )

            def update_state(data: dict):
                self._state["stage_2_transcribe"].update(data)

            try:
                result = await transcribe_stage(
                    file_id, audio_path, payload, status_callback=logger.info, state_callback=update_state
                )

                new_payload = {"video_id": result["video_id"], "title": title}
                sql = """
                    UPDATE tasks
                    SET task_type = 'stage_3_index', payload = ?, status = 'pending', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """
                with db_connection(get_sqlite_settings()) as conn:
                    conn.execute(sql, (json.dumps(new_payload), task_id))
                logger.info(f"Текст для {title} сохранен.")
            finally:
                self._state["stage_2_transcribe"].update(
                    {"active": False, "title": "", "progress": 0, "speed": "", "status_text": "Ожидание"}
                )

    async def _run_stage_3_index(self, task_id: int, payload: dict):
        async with self.embed_sem:
            video_id = payload["video_id"]
            title = payload.get("title", f"Video {video_id}")
            logger.info(f"Индексация: {title}")

            self._state["stage_3_index"].update(
                {"active": True, "title": title, "progress": 10, "status_text": "Эмбеддинги"}
            )

            try:
                settings, q_settings = get_sqlite_settings(), get_qdrant_settings()
                embed_client = UnifiedEmbeddingClient(get_embedding_settings())
                qdrant = get_qdrant_client()

                with db_connection(settings) as conn:
                    update_video_status(conn, video_id=video_id, processing_status="indexing")
                    sql_v = "SELECT title, source_file_id, source_url, recorded_date, is_short FROM videos WHERE id = ?"
                    v_row = conn.execute(sql_v, (video_id,)).fetchone()
                    sql_c = """
                        SELECT id, transcript_id, chunk_index, text, start_sec, end_sec FROM chunks
                        WHERE video_id = ? ORDER BY chunk_index ASC
                    """
                    chunks = conn.execute(sql_c, (video_id,)).fetchall()

                if not chunks:
                    with db_connection(settings) as conn:
                        update_video_status(conn, video_id=video_id, processing_status="indexed_chunks_ready")
                        conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,))
                    return

                texts = [c["text"] for c in chunks]

                def on_embed_progress(current, total):
                    # Эмбеддинги занимают диапазон 10% - 70% от общего прогресса этапа
                    progress = 10 + int((current / total) * 60)
                    self._state["stage_3_index"].update(
                        {"progress": progress, "status_text": f"Генерация ({current}/{total})"}
                    )

                embeddings_data = await embed_client.embed_batch_async(texts, progress_callback=on_embed_progress)

                self._state["stage_3_index"].update({"progress": 75, "status_text": "Загрузка в Qdrant"})
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
                                "chunk_id": row["id"],
                                "transcript_id": row["transcript_id"],
                                "chunk_index": row["chunk_index"],
                                "text": row["text"],
                                "start_sec": row["start_sec"],
                                "end_sec": row["end_sec"],
                                "video_id": video_id,
                                "title": v_row["title"],
                                "recorded_date": v_row["recorded_date"],
                                "is_short": bool(v_row["is_short"]),
                                "source_file_id": v_row["source_file_id"],
                                "is_primary": True,
                            },
                        )
                    )

                loop = asyncio.get_running_loop()
                if points:
                    await loop.run_in_executor(
                        None, lambda p=points: qdrant.upsert(collection_name=q_settings.collection_name, points=p)
                    )

                with db_connection(settings) as conn:
                    update_video_status(conn, video_id=video_id, processing_status="indexed_chunks_ready")
                    sql_f = "UPDATE tasks SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                    conn.execute(sql_f, (task_id,))
                logger.info(f"{v_row['title']} доступен для поиска.")
            finally:
                self._state["stage_3_index"].update(
                    {"active": False, "title": "", "progress": 0, "status_text": "Ожидание"}
                )

    async def _consume_stage(self, stage_types: list[str]):
        """Бесконечный цикл обработки задач определенного типа."""
        while self.is_running:
            try:
                # Атомарно помечаем задачу как запущенную и получаем её данные
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
                    RETURNING id, task_type, payload;
                """
                with db_connection(get_sqlite_settings()) as conn:
                    row = conn.execute(sql, stage_types).fetchone()

                if row:
                    tid, ttype, tpayload_json = row["id"], row["task_type"], row["payload"]
                    tpayload = json.loads(tpayload_json)

                    # Выполняем задачу
                    try:
                        if ttype in ("stage_1_download", "ingest_video"):
                            await self._run_stage_1_download(tid, tpayload)
                        elif ttype == "stage_2_transcribe":
                            await self._run_stage_2_transcribe(tid, tpayload)
                        elif ttype == "stage_3_index":
                            await self._run_stage_3_index(tid, tpayload)
                    except Exception:
                        error_trace = traceback.format_exc()
                        logger.error(f"Ошибка в задаче {tid} ({ttype}): {error_trace}")
                        sql_err = """
                            UPDATE tasks
                            SET status = 'failed', error_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """
                        with db_connection(get_sqlite_settings()) as conn:
                            conn.execute(sql_err, (error_trace, tid))
                else:
                    # Если в этой очереди задач нет, проверяем, есть ли они ВООБЩЕ в системе
                    if not await self._has_pending_tasks():
                        logger.info("Очередь пуста. Автоматическая остановка воркера для экономии ресурсов.")
                        self.is_running = False
                        break
                    # Если задачи есть в других стадиях, просто ждем
                    await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Ошибка в консьюмере {stage_types}: {e}")
                await asyncio.sleep(5)

    def cleanup(self):
        """Сброс зависших задач и очистка осиротевших временных файлов."""
        settings = get_sqlite_settings()
        from app.config import get_app_settings

        app_settings = get_app_settings()

        active_audio_paths = set()
        with db_connection(settings) as conn:
            # 1. Сброс задач, которые зависли в состоянии 'running'
            conn.execute("UPDATE tasks SET status = 'pending' WHERE status = 'running'")

            # 2. Сбор путей аудиофайлов, которые все еще нужны для активных задач
            sql = "SELECT payload FROM tasks WHERE task_type = 'stage_2_transcribe' AND status = 'pending'"
            rows = conn.execute(sql).fetchall()
            for r in rows:
                try:
                    p = json.loads(r["payload"]).get("audio_path")
                    if p:
                        active_audio_paths.add(Path(p).resolve())
                except Exception:
                    continue

        # 3. Физическая очистка папки downloads (видео не должны там лежать вне работы воркера)
        if app_settings.downloads_dir.exists():
            for p in app_settings.downloads_dir.glob("*"):
                if p.is_file():
                    try:
                        p.unlink()
                    except Exception:
                        pass

        # 4. Физическая очистка папки audio (только те, что не нужны для текущих задач)
        if app_settings.audio_dir.exists():
            for p in app_settings.audio_dir.glob("*.wav"):
                if p.resolve() not in active_audio_paths:
                    try:
                        p.unlink()
                    except Exception:
                        pass

        logger.info("Очистка временных файлов и базы данных завершена.")

    async def run(self):
        if self.is_running:
            logger.warning("Воркер уже запущен.")
            return

        self.cleanup()
        self.is_running = True
        self.is_stopping = False
        logger.info("Воркер активен: ТРЕХСТАДИЙНЫЙ ПАРАЛЛЕЛЬНЫЙ КОНВЕЙЕР")

        try:
            # Запускаем три независимых консьюмера
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


def get_worker():
    return _worker_instance
