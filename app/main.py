from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from qdrant_client import models
from starlette.middleware.sessions import SessionMiddleware

from app.auth import get_session_token, is_valid_token, login_user, logout_user, require_access_token, require_admin
from app.config import (
    get_app_settings,
    get_deepgram_settings,
    get_embedding_settings,
    get_google_drive_settings,
    get_qdrant_settings,
    get_sqlite_settings,
)
from app.db import db_connection, init_db
from app.gemini import UnifiedEmbeddingClient
from app.google_drive import GoogleDriveClient
from app.qdrant import get_qdrant_client, init_qdrant
from app.schemas import FeedbackRequest, VideoStatusItem
from app.search import hybrid_search
from app.worker import broadcaster, get_worker, set_main_loop

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))


# Cache for global stats (60s)
_global_stats_cache: dict[str, Any] = {"data": None, "timestamp": 0}


def get_global_stats() -> dict[str, Any]:
    global _global_stats_cache
    now = time.time()
    # Check tasks status every time (or with very short cache) to keep UI responsive
    settings = get_sqlite_settings()
    worker_busy = False
    try:
        with db_connection(settings) as conn:
            task_check = conn.execute("SELECT 1 FROM tasks WHERE status IN ('pending', 'running') LIMIT 1").fetchone()
            worker_busy = task_check is not None

            # Cache the rest of the stats for 60s
            if now - _global_stats_cache["timestamp"] < 60 and _global_stats_cache["data"]:
                data = _global_stats_cache["data"].copy()
                data["worker_busy"] = worker_busy
                return data

            sql = (
                "SELECT COUNT(*) as count, SUM(duration_sec) as total_sec "
                "FROM videos WHERE processing_status = 'indexed_chunks_ready'"
            )
            row = conn.execute(sql).fetchone()
            total_sec = row["total_sec"] or 0
            count = row["count"]
            hours = int(total_sec // 3600)

            _global_stats_cache["data"] = {"total_videos": count, "total_hours": hours}
            _global_stats_cache["timestamp"] = now

            data = _global_stats_cache["data"].copy()
            data["worker_busy"] = worker_busy
            return data
    except Exception:
        return {"total_videos": 0, "total_hours": 0, "worker_busy": False}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        init_db(connection)

    init_qdrant()

    # Enable thread-safe logging to WebSocket
    set_main_loop(asyncio.get_running_loop())

    # Start background worker
    worker = get_worker()
    asyncio.create_task(worker.run())

    logger.info("Application initialized with background worker.")

    yield

    # Shutdown logic (if any)
    logger.info("Shutting down...")


app = FastAPI(title="Pulsar", lifespan=lifespan)


@app.middleware("http")
async def add_global_stats_to_templates(request: Request, call_next):
    # This is a hack to make stats available in all templates without changing every route
    # FastAPI/Jinja2 doesn't have a built-in context processor like Flask/Django
    # so we manually inject it into templates.env.globals
    from typing import cast

    cast(dict, templates.env.globals)["stats"] = get_global_stats()
    response = await call_next(request)
    return response


# Session Middleware for Auth
app.add_middleware(SessionMiddleware, secret_key=get_app_settings().session_secret_key)

# Ensure required directories exist
settings = get_app_settings()
for d in [
    settings.data_dir,
    settings.storage_dir,
    settings.downloads_dir,
    settings.audio_dir,
    settings.voice_samples_dir,
    settings.raw_transcripts_dir,
    settings.normalized_transcripts_dir,
]:
    d.mkdir(parents=True, exist_ok=True)

# Static files for voice samples
app.mount("/audio", StaticFiles(directory=str(settings.voice_samples_dir)), name="voice_audio")
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")


def _status_rows(connection: Any) -> list[VideoStatusItem]:
    rows = connection.execute(
        """
        SELECT
            v.id, v.title, v.source_type, v.source_file_id, v.processing_status,
            v.local_video_path, v.local_audio_path, v.updated_at, v.created_at,
            (SELECT COUNT(*) FROM transcripts t WHERE t.video_id = v.id) AS transcript_count,
            (SELECT COUNT(*) FROM chunks c WHERE c.video_id = v.id) AS chunk_count,
            'Deepgram' as primary_engine
        FROM videos v
        ORDER BY v.created_at DESC, v.id DESC
        """
    ).fetchall()
    return [VideoStatusItem(**dict(row)) for row in rows]


@app.websocket("/api/v1/logs/stream")
async def websocket_logs(websocket: WebSocket):
    # Auth check
    token = websocket.session.get("access_token") or websocket.session.get("token")
    if not is_valid_token(token):
        # Check cookie as fallback
        token = websocket.cookies.get("access_token")
        if not is_valid_token(token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()

    q = broadcaster.register()
    logger.info(f"WebSocket client connected from {websocket.client}")
    try:
        while True:
            # Get log from queue specific to this client
            log_msg = await q.get()
            await websocket.send_text(log_msg)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        broadcaster.unregister(q)


@app.get("/api/v1/logs/poll")
async def api_poll_logs():
    """Fallback endpoint for environments where WebSockets are blocked."""
    return {"status": "polling_not_implemented", "hint": "Use WebSockets"}


@app.get("/api/v1/logs/stream")
async def websocket_logs_get():
    return {"status": "log_stream_active", "transport": "websocket_required"}


# --- SPEAKERS ROUTES ---


@app.get("/speakers", response_class=HTMLResponse)
async def speakers_page(request: Request):
    current_token = get_session_token(request)
    if not is_valid_token(current_token):
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token:
        return RedirectResponse(url="/")

    q_client = get_qdrant_client()
    try:
        res = q_client.scroll(collection_name="speaker_registry", limit=100)[0]
        speakers = []
        for p in res:
            if p.payload:
                speakers.append(
                    {
                        "id": p.id,
                        "name": p.payload.get("name", "Unknown"),
                        "audio_url": f"/audio/{p.payload.get('sample_file')}" if p.payload.get("sample_file") else None,
                    }
                )
        return templates.TemplateResponse(request, "speakers.html", {"speakers": speakers})
    except Exception as e:
        logger.error(f"Error fetching speakers: {e}")
        return templates.TemplateResponse(request, "speakers.html", {"speakers": [], "error": str(e)})


@app.post("/api/speakers/register")
async def api_register_speaker(
    video_id: int = Form(...),
    start_sec: float = Form(...),
    end_sec: float = Form(...),
    name: str = Form(...),
    _: str = Depends(require_admin),
):
    # Logic to save voice samples and embeddings is disabled to save space and avoid 405 errors
    logger.info(f"Manual speaker registration for '{name}' received but ignored (storage disabled).")
    return {"status": "success", "info": "Global enrollment disabled for now."}


@app.delete("/api/speakers/{speaker_id}")
async def api_delete_speaker(speaker_id: str, _: str = Depends(require_admin)):
    q_client = get_qdrant_client()
    settings = get_app_settings()

    # Пытаемся найти инфу о файле перед удалением
    try:
        points = q_client.retrieve(collection_name="speaker_registry", ids=[speaker_id])
        if points and points[0].payload:
            filename = points[0].payload.get("sample_file")
            if filename:
                file_path = settings.voice_samples_dir / filename
                if file_path.exists():
                    file_path.unlink()
    except Exception:
        pass

    q_client.delete(collection_name="speaker_registry", points_selector=models.PointIdsList(points=[speaker_id]))
    return {"status": "deleted"}


@app.post("/api/v1/worker/start")
async def api_worker_start(_: str = Depends(require_admin)):
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())
        return {"status": "starting"}
    return {"status": "already_running"}


@app.post("/api/v1/worker/stop")
async def api_worker_stop(_: str = Depends(require_admin)):
    worker = get_worker()
    if worker.is_running:
        worker.stop()
        return {"status": "stopping"}
    return {"status": "already_stopped"}


@app.get("/api/v1/worker/status")
async def api_worker_status(_: str = Depends(require_admin)):
    worker = get_worker()
    status = "stopped"
    if worker.is_stopping:
        status = "stopping"
    elif worker.is_running:
        status = "running"

    return {"status": status, "is_running": worker.is_running}


@app.get("/api/v1/worker/progress")
async def api_worker_progress(_: str = Depends(require_admin)):
    """Returns real-time progress for all stages and queue counts."""
    worker = get_worker()
    state = worker.get_progress_state()

    settings = get_sqlite_settings()
    counts = {"stage_1_download": 0, "stage_2_transcribe": 0, "stage_3_index": 0}
    stats = {"pending": 0, "failed": 0, "recent_errors": []}

    with db_connection(settings) as conn:
        # Get pending tasks per stage
        sql_q = "SELECT task_type, COUNT(*) as c FROM tasks WHERE status = 'pending' GROUP BY task_type"
        rows = conn.execute(sql_q).fetchall()
        for r in rows:
            ttype = r["task_type"]
            if ttype == "ingest_video":
                ttype = "stage_1_download"
            if ttype in counts:
                counts[ttype] += r["c"]

        # Get aggregate stats
        sql_s = "SELECT status, COUNT(*) as c FROM tasks GROUP BY status"
        s_rows = conn.execute(sql_s).fetchall()
        stats["skipped_silent_list"] = []
        stats["skipped_no_space_list"] = []
        for r in s_rows:
            if r["status"] == "pending":
                stats["pending"] = r["c"]
            if r["status"] == "failed":
                stats["failed"] = r["c"]
            if r["status"] == "skipped_silent":
                stats["skipped_silent"] = r["c"]
            if r["status"] == "skipped_no_space":
                stats["skipped_no_space"] = r["c"]

        # Detail skipped silent
        skipped_silent_count = stats.get("skipped_silent", 0)
        if isinstance(skipped_silent_count, int) and skipped_silent_count > 0:
            sql_ss = "SELECT id, payload FROM tasks WHERE status = 'skipped_silent' ORDER BY updated_at DESC LIMIT 20"
            ss_rows = conn.execute(sql_ss).fetchall()
            for ssr in ss_rows:
                try:
                    p = json.loads(ssr["payload"])
                    stats["skipped_silent_list"].append({"id": ssr["id"], "title": p.get("title") or "Unknown"})
                except Exception:
                    pass

        # Detail skipped no space
        skipped_no_space_count = stats.get("skipped_no_space", 0)
        if isinstance(skipped_no_space_count, int) and skipped_no_space_count > 0:
            sql_ns = "SELECT id, payload FROM tasks WHERE status = 'skipped_no_space' ORDER BY updated_at DESC LIMIT 20"
            ns_rows = conn.execute(sql_ns).fetchall()
            for nsr in ns_rows:
                try:
                    p = json.loads(nsr["payload"])
                    size_gb = p.get("file_size", 0) / (1024**3)
                    stats["skipped_no_space_list"].append(
                        {"id": nsr["id"], "title": p.get("title") or "Unknown", "size": f"{size_gb:.2f} ГБ"}
                    )
                except Exception:
                    pass

        # Detail missing on Google Drive
        stats["missing_on_drive_list"] = []
        stats["missing_on_drive_count"] = 0
        sql_mc = "SELECT COUNT(*) as c FROM videos WHERE is_missing = 1"
        stats["missing_on_drive_count"] = conn.execute(sql_mc).fetchone()["c"]
        if stats["missing_on_drive_count"] > 0:
            sql_m = (
                "SELECT id, title, source_file_id, source_url "
                "FROM videos WHERE is_missing = 1 "
                "ORDER BY updated_at DESC LIMIT 20"
            )
            m_rows = conn.execute(sql_m).fetchall()
            for mr in m_rows:
                stats["missing_on_drive_list"].append(
                    {
                        "id": mr["id"],
                        "title": mr["title"],
                        "file_id": mr["source_file_id"],
                        "source_url": mr["source_url"],
                    }
                )

        # Detail excluded by keyword
        stats["excluded_by_keyword_list"] = []
        stats["excluded_by_keyword_count"] = 0
        sql_ec = "SELECT COUNT(*) as c FROM videos WHERE is_excluded = 1"
        stats["excluded_by_keyword_count"] = conn.execute(sql_ec).fetchone()["c"]
        if stats["excluded_by_keyword_count"] > 0:
            sql_ex = (
                "SELECT id, title, source_file_id, source_url "
                "FROM videos WHERE is_excluded = 1 "
                "ORDER BY updated_at DESC LIMIT 20"
            )
            ex_rows = conn.execute(sql_ex).fetchall()
            for exr in ex_rows:
                stats["excluded_by_keyword_list"].append(
                    {
                        "id": exr["id"],
                        "title": exr["title"],
                        "file_id": exr["source_file_id"],
                        "source_url": exr["source_url"],
                    }
                )

        # Detail duplicate MD5 files
        stats["duplicate_md5_list"] = []
        stats["duplicate_md5_count"] = 0
        sql_dc = "SELECT COUNT(*) as c FROM videos WHERE is_md5_duplicate = 1"
        stats["duplicate_md5_count"] = conn.execute(sql_dc).fetchone()["c"]
        if stats["duplicate_md5_count"] > 0:
            sql_d = (
                "SELECT id, title, source_file_id, source_url, md5_checksum "
                "FROM videos WHERE is_md5_duplicate = 1 "
                "ORDER BY updated_at DESC LIMIT 20"
            )
            d_rows = conn.execute(sql_d).fetchall()
            for dr in d_rows:
                # Find matching original video
                original_title = "Неизвестный оригинал"
                if dr["md5_checksum"]:
                    sql_orig = "SELECT title FROM videos WHERE md5_checksum = ? AND is_md5_duplicate = 0 LIMIT 1"
                    orig_row = conn.execute(sql_orig, (dr["md5_checksum"],)).fetchone()
                    if orig_row:
                        original_title = orig_row["title"]
                stats["duplicate_md5_list"].append(
                    {
                        "id": dr["id"],
                        "title": dr["title"],
                        "file_id": dr["source_file_id"],
                        "source_url": dr["source_url"],
                        "original_title": original_title,
                    }
                )

        # Detail failed
        if stats["failed"] > 0:
            sql_e = """
                SELECT id, task_type, error_message, payload
                FROM tasks WHERE status = 'failed'
                ORDER BY updated_at DESC LIMIT 5
            """
            e_rows = conn.execute(sql_e).fetchall()
            for er in e_rows:
                title = "Unknown Task"
                try:
                    p = json.loads(er["payload"])
                    title = p.get("title") or p.get("file_id") or "Task"
                except Exception:
                    pass

                stats["recent_errors"].append(
                    {"id": er["id"], "title": title, "type": er["task_type"], "error": er["error_message"]}
                )

    # Get balance from cache
    from app.transcription.deepgram import DeepgramEngine

    dg_engine = DeepgramEngine(get_deepgram_settings())
    balance_data = dg_engine.get_balance()

    return {
        "stages": state,
        "queue_counts": counts,
        "stats": stats,
        "is_running": worker.is_running,
        "dg_balance": balance_data,
    }


@app.post("/api/v1/tasks/{task_id}/restart")
async def api_restart_task(task_id: int, _: str = Depends(require_admin)):
    """Restart a specific task (failed or skipped)."""
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        conn.execute("UPDATE tasks SET status = 'pending', error_message = NULL WHERE id = ?", (task_id,))

    # Auto-start worker
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "restarted"}


@app.get("/api/v1/deepgram/balance")
async def api_deepgram_balance(_: str = Depends(require_admin)):
    """Fetch Deepgram balance info."""
    from app.transcription.deepgram import DeepgramEngine

    settings = get_deepgram_settings()
    engine = DeepgramEngine(settings)
    return engine.get_balance()


@app.post("/api/tasks/ingest")
async def api_add_ingest_task(
    file_id: str = Form(...),
    title: str = Form(None),
    diarize: bool = Form(True),
    _: str = Depends(require_admin),
):
    drive_client = GoogleDriveClient(get_google_drive_settings())
    try:
        file_meta = await drive_client.get_file(file_id)
        md5 = file_meta.md5_checksum
    except Exception as e:
        logger.error(f"Failed to get file metadata for {file_id}: {e}")
        md5 = None

    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        if md5:
            # Check for content duplicates
            existing = conn.execute("SELECT id FROM videos WHERE md5_checksum = ?", (md5,)).fetchone()
            if existing:
                return {"status": "already_indexed", "video_id": existing["id"]}

        conn.execute(
            "INSERT INTO tasks (task_type, payload) VALUES (?, ?)",
            ("stage_1_download", json.dumps({"file_id": file_id, "title": title, "diarize": diarize, "md5": md5})),
        )

    # Auto-start worker
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "queued", "file_id": file_id}


@app.get("/indexed", response_class=HTMLResponse)
def indexed_page(request: Request):
    current_token = get_session_token(request)
    if not is_valid_token(current_token):
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token:
        return RedirectResponse(url="/")

    return templates.TemplateResponse(request, "indexed.html", {})


@app.post("/api/v1/indexed/videos/{video_id}/toggle_short")
async def api_toggle_short(video_id: int, _: str = Depends(require_admin)):
    """Toggles is_short status and re-queues for indexing."""
    pg_settings = get_sqlite_settings()
    q_settings = get_qdrant_settings()

    from app.chunking import chunk_from_utterances
    from app.qdrant import get_qdrant_client
    from app.repository import replace_chunks

    with db_connection(pg_settings) as conn:
        # 1. Toggle status
        row = conn.execute("SELECT is_short, title FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Video not found")

        new_is_short = not bool(row["is_short"])
        conn.execute("UPDATE videos SET is_short = ? WHERE id = ?", (new_is_short, video_id))

        # 2. Get transcript to re-chunk
        t_row = conn.execute(
            "SELECT id, normalized_json_path FROM transcripts WHERE video_id = ?", (video_id,)
        ).fetchone()
        if t_row and t_row["normalized_json_path"]:
            norm_path = Path(t_row["normalized_json_path"])
            if norm_path.exists():
                norm_payload = json.loads(norm_path.read_text(encoding="utf-8"))
                raw_chunks = norm_payload.get("utterances") or norm_payload.get("chunks") or []

                # 3. Re-chunk
                new_chunks = chunk_from_utterances(raw_chunks, single_chunk=new_is_short)

                # 4. Clear Qdrant old points
                old_chunk_ids = [
                    r["id"] for r in conn.execute("SELECT id FROM chunks WHERE video_id = ?", (video_id,)).fetchall()
                ]
                if old_chunk_ids:
                    qdrant = get_qdrant_client()
                    qdrant.delete(
                        collection_name=q_settings.collection_name,
                        points_selector=models.PointIdsList(points=old_chunk_ids),
                    )

                # 5. Clear SQLite chunks and save new ones
                replace_chunks(conn, video_id=video_id, transcript_id=t_row["id"], chunks=new_chunks)

                # 6. Re-queue for Stage 3 indexing
                conn.execute(
                    "INSERT INTO tasks (task_type, payload) VALUES (?, ?)",
                    ("stage_3_index", json.dumps({"video_id": video_id})),
                )

                # Auto-start worker if needed
                worker = get_worker()
                if not worker.is_running:
                    asyncio.create_task(worker.run())

        conn.commit()

    return {"status": "ok", "is_short": new_is_short, "queued": True}


@app.get("/api/v1/indexed/ls")
async def api_indexed_ls(folder_id: str | None = None, _: str = Depends(require_admin)):
    """Lists indexed folders and videos from local DB with metadata."""
    pg_settings = get_sqlite_settings()
    target_id = folder_id if folder_id and folder_id != "root" else None

    with db_connection(pg_settings) as connection:
        # 1. Get subfolders
        if target_id:
            sql_f = "SELECT id, name FROM folders WHERE parent_id = ? ORDER BY name ASC"
            f_rows = connection.execute(sql_f, (target_id,)).fetchall()
        else:
            sql_f = """
                SELECT id, name FROM folders
                WHERE parent_id IS NULL OR parent_id NOT IN (SELECT id FROM folders)
                ORDER BY name ASC
                """
            f_rows = connection.execute(sql_f).fetchall()

        # 2. Get videos in this folder with rich metadata
        video_sql = """
            SELECT
                v.id, v.title, v.mime_type, v.duration_sec, v.updated_at, v.source_file_id, v.is_short, v.is_4k,
                t.language, t.confidence,
                (SELECT COUNT(*) FROM chunks c WHERE c.video_id = v.id) as chunk_count
            FROM videos v
            LEFT JOIN transcripts t ON t.video_id = v.id
            WHERE {where_clause}
            ORDER BY v.title ASC
        """

        if target_id:
            where = "v.parent_folder_id = ?"
            params = (target_id,)
        else:
            where = "v.parent_folder_id IS NULL OR v.parent_folder_id NOT IN (SELECT id FROM folders)"
            params = ()

        v_rows = connection.execute(video_sql.format(where_clause=where), params).fetchall()

        # 3. Get current folder path (breadcrumbs)
        path = []
        curr = target_id
        while curr:
            row = connection.execute("SELECT id, name, parent_id FROM folders WHERE id = ?", (curr,)).fetchone()
            if row:
                path.append({"id": row["id"], "name": row["name"]})
                curr = str(row["parent_id"]) if row["parent_id"] else None
            else:
                break
        path.reverse()

    items = []
    for r in f_rows:
        items.append({"id": r["id"], "name": r["name"], "is_folder": True, "mime_type": "folder"})

    for r in v_rows:
        items.append(
            {
                "id": r["id"],
                "name": r["title"],
                "is_folder": False,
                "mime_type": r["mime_type"],
                "is_short": bool(r["is_short"]),
                "is_4k": bool(r["is_4k"]),
                "source_file_id": r["source_file_id"],
                "duration_sec": r["duration_sec"],
                "chunk_count": r["chunk_count"],
                "language": r["language"],
                "confidence": r["confidence"],
                "updated_at": r["updated_at"],
            }
        )

    return {"items": items, "path": path}


@app.post("/api/v1/indexed/mkdir")
async def api_indexed_mkdir(name: str = Form(...), parent_id: str | None = Form(None), _: str = Depends(require_admin)):
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное управление структурой папок отключено. Структура синхронизируется автоматически с Google Drive.",
    )


@app.post("/api/v1/indexed/move")
async def api_indexed_move(
    video_id: int = Form(...), folder_id: str | None = Form(None), _: str = Depends(require_admin)
):
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное перемещение файлов отключено. Структура синхронизируется автоматически с Google Drive.",
    )


@app.post("/api/v1/indexed/folders/rename")
async def api_indexed_rename_folder(
    folder_id: str = Form(...), new_name: str = Form(...), _: str = Depends(require_admin)
):
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное переименование папок отключено. Структура синхронизируется автоматически с Google Drive.",
    )


@app.delete("/api/v1/indexed/folders/{folder_id}")
async def api_indexed_delete_folder(folder_id: str, _: str = Depends(require_admin)):
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное удаление папок отключено. Структура синхронизируется автоматически с Google Drive.",
    )
    return {"status": "success"}


@app.post("/api/v1/indexed/sync")
async def api_indexed_sync(_: str = Depends(require_admin)):
    """Trigger metadata synchronization for all indexed files."""
    from scripts.sync_titles import sync_indexed_metadata

    try:
        count = await sync_indexed_metadata()
        return {"status": "success", "updated_count": count}
    except Exception as e:
        logger.error(f"Metadata sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/manifest.json")
async def manifest():
    return {
        "name": "Pulsar AI",
        "short_name": "Pulsar",
        "description": "Корпоративный поиск по видео-архиву",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#FAF9F6",
        "theme_color": "#5F6683",
        "icons": [
            {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
    }


@app.get("/sw.js")
async def service_worker():
    content = """
self.addEventListener('install', (e) => {
  self.skipWaiting();
});
self.addEventListener('fetch', (event) => {
  // Pass-through for now
  event.respondWith(fetch(event.request));
});
"""
    return Response(content=content, media_type="application/javascript")


# --- AUTH ROUTES ---


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login_post(
    request: Request,
    response: Response,
    token: str | None = Form(None),
    email: str | None = Form(None),
    code: str | None = Form(None),
):
    # 1. Fallback / Static access token login
    if token:
        if login_user(response, request, token):
            return RedirectResponse(url="/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid access token"})

    # 2. Ark Messenger 2-step login (email + code)
    if email and code:
        app_settings = get_app_settings()
        if not app_settings.ark_jwks_url:
            return templates.TemplateResponse(
                request, "login.html", {"error": "Ark Messenger authentication is not configured"}
            )

        base_url = app_settings.ark_jwks_url.rsplit("/.well-known/jwks.json", 1)[0]
        verify_url = f"{base_url}/api/v1/auth/verify-code"

        async with httpx.AsyncClient() as client:
            try:
                res = await client.post(verify_url, json={"email": email, "code": code}, timeout=10.0)
                if res.status_code == 200:
                    data = res.json()
                    next_step = data.get("next")
                    if next_step == "home":
                        access_token = data.get("access_token")
                        if access_token and login_user(response, request, access_token):
                            return RedirectResponse(url="/", status_code=303)
                        else:
                            return templates.TemplateResponse(
                                request, "login.html", {"error": "Failed to log in with returned token"}
                            )
                    elif next_step == "setup_profile":
                        return templates.TemplateResponse(
                            request,
                            "login.html",
                            {
                                "error": (
                                    "Профиль еще не заполнен. Пожалуйста, завершите настройку профиля в мессенджере."
                                ),
                                "email": email,
                                "code": code,
                            },
                        )
                    else:
                        return templates.TemplateResponse(
                            request, "login.html", {"error": f"Unexpected login state: {next_step}"}
                        )
                elif res.status_code == 401:
                    try:
                        detail = res.json().get("detail", "Неверный пинкод или email")
                    except Exception:
                        detail = "Неверный пинкод или email"
                    return templates.TemplateResponse(
                        request, "login.html", {"error": detail, "email": email, "code": code}
                    )
                else:
                    return templates.TemplateResponse(
                        request, "login.html", {"error": f"Ошибка сервера авторизации: {res.status_code}"}
                    )
            except Exception as e:
                logger.error(f"Error calling Ark Messenger verify-code: {e}")
                return templates.TemplateResponse(
                    request, "login.html", {"error": "Не удалось связаться с сервером авторизации Ark Messenger"}
                )

    return templates.TemplateResponse(request, "login.html", {"error": "Не указаны учетные данные"})


@app.post("/api/v1/auth/identify")
async def api_auth_identify(request: Request):
    app_settings = get_app_settings()
    if not app_settings.ark_jwks_url:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Ark Messenger authentication is not configured"
        )

    base_url = app_settings.ark_jwks_url.rsplit("/.well-known/jwks.json", 1)[0]
    identify_url = f"{base_url}/api/v1/auth/identify"

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from e

    email = body.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Missing email field")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(identify_url, json={"email": email}, timeout=10.0)
            return Response(content=response.content, status_code=response.status_code, media_type="application/json")
        except Exception as e:
            logger.error(f"Error calling Ark Messenger identify: {e}")
            raise HTTPException(status_code=502, detail="Error communicating with Ark Messenger server") from e


@app.get("/logout")
def logout(request: Request, response: Response):
    logout_user(response, request)
    return RedirectResponse(url="/login")


@app.post("/api/v1/webhooks/revocation")
async def handle_revocation_webhook(request: Request):
    import hashlib
    import hmac

    from app.auth import revoke_session, revoke_user

    app_settings = get_app_settings()

    # 1. Check if webhook secret is configured
    webhook_secret = app_settings.ark_webhook_secret
    if not webhook_secret:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Webhook revocation is not configured")

    # 2. Get raw request body for signature verification
    payload = await request.body()

    # 3. Extract signature from headers
    signature = request.headers.get("X-Ark-Signature")
    if not signature:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing signature header")

    # 4. Calculate expected signature
    expected_signature = hmac.new(webhook_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    # 5. Securely compare signatures
    if not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    # 6. Parse JSON payload and revoke session/user
    try:
        data = json.loads(payload)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON payload: {str(e)}") from e

    user_id = data.get("user_id")
    jti = data.get("jti")

    if not user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing user_id in payload")

    if jti:
        revoke_session(jti)
    else:
        revoke_user(user_id)

    return {"status": "ok"}


# --- UI ROUTES ---


@app.get("/", response_class=HTMLResponse)
async def index_page(
    request: Request,
    q: str | None = None,
    mode: str = "hybrid",
    date_from: str | None = None,
    date_to: str | None = None,
    video_type: str = "all",
):
    app_settings = get_app_settings()
    pg_settings = get_sqlite_settings()

    # URL Token Auth
    token_param = request.query_params.get("token")
    allowed_modes = {"hybrid", "semantic", "keyword"}
    safe_mode = mode if mode in allowed_modes else "hybrid"
    if token_param and is_valid_token(token_param):
        redirect_url = request.url_for("index_page").include_query_params(q=q or "", mode=safe_mode)
        response = RedirectResponse(url=str(redirect_url))
        login_user(response, request, str(token_param))
        return response

    current_token = get_session_token(request)
    if not is_valid_token(current_token):
        return RedirectResponse(url="/login")

    results = []
    # Fetch results if there is a query OR if filters are applied
    if q or date_from or date_to or video_type != "all":
        with db_connection(pg_settings) as connection:
            items = await hybrid_search(
                connection,
                q or "",  # Pass empty string if None
                limit=app_settings.results_limit,
                search_mode=mode,
                date_from=date_from,
                date_to=date_to,
                video_type=video_type,
            )
            results = items

    ua = request.headers.get("user-agent", "").lower()
    is_mobile = any(m in ua for m in ["mobile", "android", "iphone", "ipad"])
    template = "index_mobile.html" if is_mobile else "index.html"

    from datetime import date

    today = date.today().isoformat()
    default_start = "2020-01-01"

    return templates.TemplateResponse(
        request,
        template,
        {
            "query": q or "",
            "results": results,
            "mode": mode,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "today_val": today,
            "default_start": default_start,
            "video_type": video_type,
            "token": current_token,
        },
    )


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    current_token = get_session_token(request)
    if not is_valid_token(current_token):
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token:
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "import.html", {})


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    pg_settings = get_sqlite_settings()

    current_token = get_session_token(request)
    if not is_valid_token(current_token):
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token:
        return RedirectResponse(url="/")

    with db_connection(pg_settings) as connection:
        statuses = _status_rows(connection)

        # Helper to process task rows
        def process_tasks(rows):
            processed = []
            for row in rows:
                t = dict(row)
                try:
                    payload = json.loads(t["payload"])
                    t["file_id"] = payload.get("file_id")
                    if t["file_id"]:
                        sql_v = "SELECT title FROM videos WHERE source_file_id = ?"
                        v = connection.execute(sql_v, (t["file_id"],)).fetchone()
                        t["title"] = v["title"] if v else f"Файл {t['file_id'][:8]}..."
                    else:
                        t["title"] = payload.get("title", "AI Indexing")
                except Exception:
                    t["title"] = "Task"
                processed.append(t)
            return processed

        # 1. Fetch ALL running tasks
        sql_running = "SELECT * FROM tasks WHERE status = 'running' ORDER BY created_at ASC"
        running_rows = connection.execute(sql_running).fetchall()
        active_tasks = process_tasks(running_rows)

        # 2. Fetch recent queue (limit 50)
        sql_recent = """
            SELECT * FROM tasks
            WHERE status IN ('pending', 'running', 'failed')
            ORDER BY created_at DESC LIMIT 50
        """
        recent_rows = connection.execute(sql_recent).fetchall()
        tasks = process_tasks(recent_rows)

    return templates.TemplateResponse(
        request, "status.html", {"statuses": statuses, "tasks": tasks, "active_tasks": active_tasks}
    )


@app.get("/feedback", response_class=HTMLResponse)
def feedback_page(request: Request):
    current_token = get_session_token(request)
    if not is_valid_token(current_token):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "feedback.html", {"stats": get_global_stats()})


...


@app.post("/api/v1/feedback")
async def api_submit_feedback(req: FeedbackRequest, _: str = Depends(require_access_token)):
    app_settings = get_app_settings()
    if not app_settings.github_pat:
        raise HTTPException(status_code=500, detail="GitHub PAT не настроен в .env")

    url = f"https://api.github.com/repos/{app_settings.github_repo}/issues"
    headers = {
        "Authorization": f"Bearer {app_settings.github_pat}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Pulsar-AI",
    }

    body_md = f"**Описание:**\n{req.description}\n\n---\n*Отправлено через Pulsar AI Feedback Form*"

    payload = {"title": req.title, "body": body_md, "labels": ["feedback"]}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=payload, timeout=20.0)
            if response.status_code == 201:
                return {"status": "success", "issue_url": response.json().get("html_url")}
            else:
                logger.error(f"GitHub API Error ({response.status_code}): {response.text}")
                try:
                    err_data = response.json()
                    detail = err_data.get("message", response.text)
                except Exception:
                    detail = response.text
                raise HTTPException(status_code=response.status_code, detail=f"Ошибка GitHub API: {detail}")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to submit feedback to GitHub: {e}")
            raise HTTPException(status_code=500, detail=f"Ошибка сети или API: {str(e)}") from e


@app.get("/api/v1/tasks/active")
async def api_get_active_tasks(_: str = Depends(require_access_token)):
    """Returns currently running tasks for UI updates."""
    pg_settings = get_sqlite_settings()
    with db_connection(pg_settings) as connection:
        sql_running = "SELECT * FROM tasks WHERE status = 'running' ORDER BY created_at ASC"
        rows = connection.execute(sql_running).fetchall()

        processed = []
        for row in rows:
            t = dict(row)
            try:
                payload = json.loads(t["payload"])
                t["file_id"] = payload.get("file_id")
                if t["file_id"]:
                    sql_v = "SELECT title FROM videos WHERE source_file_id = ?"
                    v = connection.execute(sql_v, (t["file_id"],)).fetchone()
                    t["title"] = v["title"] if v else f"Файл {t['file_id'][:8]}..."
                else:
                    t["title"] = payload.get("title", "AI Indexing")
            except Exception:
                t["title"] = "Task"
            processed.append(t)
        return processed


@app.delete("/api/v1/tasks/{task_id}")
async def api_delete_task(task_id: int, _: str = Depends(require_access_token)):
    """Delete a task from the queue."""
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"status": "deleted"}


@app.post("/api/v1/tasks/restart_failed")
async def api_restart_failed_tasks(_: str = Depends(require_access_token)):
    """Restart all failed tasks."""
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        res = conn.execute("UPDATE tasks SET status = 'pending', error_message = NULL WHERE status = 'failed'")
        count = res.rowcount

    # Auto-start worker
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "restarted", "count": count}


@app.post("/api/v1/tasks/restart_no_space")
async def api_restart_no_space_tasks(_: str = Depends(require_access_token)):
    """Restart all tasks that were skipped due to lack of space."""
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        res = conn.execute(
            "UPDATE tasks SET status = 'pending', error_message = NULL WHERE status = 'skipped_no_space'"
        )
        count = res.rowcount

    # Auto-start worker
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "restarted", "count": count}


@app.post("/api/v1/reindex/all")
async def api_reindex_all(clear_qdrant: bool = False, _: str = Depends(require_access_token)):
    """Queues all transcribed videos for re-indexing in Qdrant."""
    settings = get_sqlite_settings()
    q_settings = get_qdrant_settings()

    if clear_qdrant:
        from app.qdrant import init_qdrant

        qdrant = get_qdrant_client()
        logger.info(f"Clearing Qdrant collection {q_settings.collection_name} for full reindex")
        try:
            qdrant.delete_collection(q_settings.collection_name)
            init_qdrant()
        except Exception as e:
            logger.error(f"Failed to clear Qdrant: {e}")

    with db_connection(settings) as conn:
        # Find all videos that have at least one chunk
        rows = conn.execute("""
            SELECT DISTINCT video_id, title
            FROM chunks c
            JOIN videos v ON v.id = c.video_id
        """).fetchall()

        count = 0
        for r in rows:
            vid = r["video_id"]
            title = r["title"]
            payload = json.dumps({"video_id": vid, "title": title})

            # Check if task already exists to avoid duplicates
            sql_check = """
                SELECT 1 FROM tasks
                WHERE task_type = 'stage_3_index' AND status IN ('pending', 'running')
                AND payload LIKE ?
            """
            exists = conn.execute(sql_check, (f'%"video_id": {vid}%',)).fetchone()

            if not exists:
                conn.execute(
                    "INSERT INTO tasks (task_type, payload, status, priority) VALUES (?, ?, ?, ?)",
                    ("stage_3_index", payload, "pending", 10),  # Higher priority for reindex
                )
                count += 1

    # Auto-start worker
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "queued", "count": count}


@app.get("/api/videos/{video_id}/speakers")
def api_list_speakers(video_id: int, _: str = Depends(require_access_token)):
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        # Find all unique speaker tags in chunks
        rows = conn.execute("SELECT DISTINCT speaker_tags FROM chunks WHERE video_id = ?", (video_id,)).fetchall()

        tags = set()
        for r in rows:
            if r["speaker_tags"]:
                for t in r["speaker_tags"].split(", "):
                    tags.add(t)

        # If no tags detected (old video or single speaker), provide a default tag 'primary'
        if not tags:
            tags.add("primary")

        # Get existing names from speakers table
        names_rows = conn.execute("SELECT speaker_tag, name FROM speakers WHERE video_id = ?", (video_id,)).fetchall()
        name_map = {r["speaker_tag"]: r["name"] for r in names_rows}

        return [
            {"tag": t, "name": name_map.get(t, f"Speaker {t}" if t != "primary" else "Основной голос")}
            for t in sorted(tags)
        ]


@app.post("/api/videos/{video_id}/speakers")
async def api_save_speaker(video_id: int, request: Request, _: str = Depends(require_access_token)):
    data = await request.json()
    tag = data.get("tag")
    name = data.get("name")

    if not tag or not name:
        raise HTTPException(status_code=400, detail="Missing tag or name")

    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        # 1. Save to local SQLite
        sql_s = """
            INSERT INTO speakers (video_id, speaker_tag, name)
            VALUES (?, ?, ?)
            ON CONFLICT(video_id, speaker_tag) DO UPDATE SET name = EXCLUDED.name
        """
        conn.execute(sql_s, (video_id, str(tag), name))

    # 2. Global Enrollment (Voice Fingerprinting) - DISABLED due to 405 error
    return {"status": "saved", "enrolled": False}


@app.get("/api/videos/{video_id}/export")
def api_video_export(video_id: int, _: str = Depends(require_access_token)):
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT c.text, c.speaker_tags
            FROM chunks c
            WHERE c.video_id = ?
            ORDER BY c.chunk_index ASC
            """,
            (video_id,),
        ).fetchall()

        if not rows:
            raise HTTPException(status_code=404, detail="Transcript not found")

        # Get speaker names for this video
        s_rows = connection.execute("SELECT speaker_tag, name FROM speakers WHERE video_id = ?", (video_id,)).fetchall()
        s_map = {r["speaker_tag"]: r["name"] for r in s_rows}

        full_text_lines = []
        for r in rows:
            speaker_info = ""
            if r["speaker_tags"]:
                names = [s_map.get(t, f"Speaker {t}") for t in r["speaker_tags"].split(", ")]
                speaker_info = f"[{', '.join(names)}]: "
            full_text_lines.append(f"{speaker_info}{r['text']}")

        full_text = "\n\n".join(full_text_lines)
        v_row = connection.execute("SELECT title FROM videos WHERE id = ?", (video_id,)).fetchone()
        filename = f"{v_row['title']}.txt" if v_row else f"transcript_{video_id}.txt"

        return Response(
            content=full_text,
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )


@app.get("/api/videos/{video_id}/chunks")
def api_video_chunks(video_id: int, _: str = Depends(require_access_token)):
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT c.id, c.start_sec, c.end_sec, c.text
            FROM chunks c
            JOIN transcripts t ON t.id = c.transcript_id
            WHERE c.video_id = ?
            ORDER BY c.chunk_index ASC
            """,
            (video_id,),
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/videos")
def api_list_videos(_: str = Depends(require_access_token)):
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        rows = connection.execute("SELECT id, title FROM videos ORDER BY title ASC").fetchall()
        return [dict(row) for row in rows]


@app.post("/api/chunks/{chunk_id}")
async def api_update_chunk(chunk_id: int, request: Request, _: str = Depends(require_access_token)):
    data = await request.json()
    new_text = data.get("text")
    if new_text is None:
        raise HTTPException(status_code=400, detail="Missing text")

    settings = get_sqlite_settings()
    embed_client = UnifiedEmbeddingClient(get_embedding_settings())
    qdrant = get_qdrant_client()
    q_settings = get_qdrant_settings()

    with db_connection(settings) as connection:
        # 1. Получаем информацию о чанке (нужна для payload в Qdrant)
        row = connection.execute(
            """
            SELECT
                c.chunk_index, c.video_id, c.transcript_id, c.start_sec, c.end_sec,
                t.normalized_json_path,
                v.title, v.source_file_id, v.source_url
            FROM chunks c
            JOIN transcripts t ON t.id = c.transcript_id
            JOIN videos v ON v.id = c.video_id
            WHERE c.id = ?
            """,
            (chunk_id,),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Chunk not found")

        # 2. Обновляем PostgreSQL (только текст, векторы ушли в Qdrant)
        connection.execute("UPDATE chunks SET text = ? WHERE id = ?", (new_text, chunk_id))

        # 3. Генерируем НОВЫЕ векторы
        try:
            dense_vec, sparse_vec = await embed_client.embed_text_async(new_text, task_type="RETRIEVAL_DOCUMENT")

            # 4. Обновляем Qdrant
            vectors: dict[str, Any] = {"default": dense_vec}
            if sparse_vec:
                vectors["text-sparse"] = sparse_vec

            qdrant.upsert(
                collection_name=q_settings.collection_name,
                points=[
                    models.PointStruct(
                        id=chunk_id,
                        vector=vectors,
                        payload={
                            "chunk_id": chunk_id,
                            "video_id": row["video_id"],
                            "transcript_id": row["transcript_id"],
                            "chunk_index": row["chunk_index"],
                            "start_sec": row["start_sec"],
                            "end_sec": row["end_sec"],
                            "text": new_text,
                            "title": row["title"],
                            "source_file_id": row["source_file_id"],
                            "source_url": row["source_url"],
                            "is_primary": True,
                        },
                    )
                ],
            )
            vector_updated = True
        except Exception as e:
            logger.error(f"Failed to update vectors in Qdrant for chunk {chunk_id}: {e}")
            vector_updated = False

        # 5. Обновляем физический файл на диске
        json_path_str = row["normalized_json_path"]
        if json_path_str:
            path = Path(json_path_str)
            if path.exists():
                try:
                    content = json.loads(path.read_text(encoding="utf-8"))
                    chunk_index = row["chunk_index"]
                    if "utterances" in content and len(content["utterances"]) > chunk_index:
                        content["utterances"][chunk_index]["text"] = new_text
                    elif "chunks" in content and len(content["chunks"]) > chunk_index:
                        content["chunks"][chunk_index]["text"] = new_text

                    if "utterances" in content:
                        content["transcript"] = " ".join(u["text"] for u in content["utterances"])

                    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as e:
                    logger.error(f"Failed to sync JSON file {path}: {e}")

    return {"status": "updated", "vector_updated": vector_updated}


@app.get("/api/drive/ls")
async def api_drive_ls(folder_id: str | None = None, refresh: bool = False, _: str = Depends(require_access_token)):
    drive_client = GoogleDriveClient(get_google_drive_settings())
    target_id = folder_id or "root"

    try:
        items = await drive_client.list_folder_contents(target_id, use_cache=not refresh)

        # Check database for indexed status
        sqlite_settings = get_sqlite_settings()
        with db_connection(sqlite_settings) as connection:
            # Get all source_file_ids for google_drive source
            indexed_rows = connection.execute(
                "SELECT source_file_id, md5_checksum FROM videos WHERE source_type = 'google_drive'"
            ).fetchall()
            indexed_ids = {row["source_file_id"] for row in indexed_rows}
            indexed_md5s = {row["md5_checksum"] for row in indexed_rows if row["md5_checksum"]}

            # Also check if folders are indexed
            folder_rows = connection.execute("SELECT id FROM folders").fetchall()
            indexed_folder_ids = {row["id"] for row in folder_rows}

            sql_q = """
                SELECT json_extract(payload, '$.file_id') as file_id,
                       json_extract(payload, '$.video_id') as video_id
                FROM tasks WHERE status IN ('pending', 'running')
            """
            queued_rows = connection.execute(sql_q).fetchall()

            queued_ids = set()
            video_ids_in_queue = []
            for r in queued_rows:
                if r["file_id"]:
                    queued_ids.add(r["file_id"])
                if r["video_id"]:
                    video_ids_in_queue.append(r["video_id"])

            if video_ids_in_queue:
                placeholders = ",".join(["?"] * len(video_ids_in_queue))
                src_ids = connection.execute(
                    f"SELECT source_file_id FROM videos WHERE id IN ({placeholders})", video_ids_in_queue
                ).fetchall()
                for s in src_ids:
                    if s["source_file_id"]:
                        queued_ids.add(s["source_file_id"])

        for item in items:
            if item.get("is_folder"):
                item["is_indexed"] = item["id"] in indexed_folder_ids
            else:
                # Check by ID or by MD5 (deduplication)
                item["is_indexed"] = (item["id"] in indexed_ids) or (item.get("md5_checksum") in indexed_md5s)

            item["is_queued"] = item["id"] in queued_ids

        return items
    except Exception as e:
        # If it's an auth error, we return a specific hint for the UI
        if "token" in str(e).lower() or "auth" in str(e).lower():
            raise HTTPException(
                status_code=401, detail="Google Drive access error. Check service account permissions."
            ) from e
        raise HTTPException(status_code=500, detail=str(e)) from e


# --- API & STREAMING ---


@app.get("/videos/{video_id}/file")
async def video_file(video_id: int, request: Request, token: str | None = None) -> Response:
    current_token = get_session_token(request) or token
    if not is_valid_token(current_token):
        raise HTTPException(status_code=401)

    pg_settings = get_sqlite_settings()
    with db_connection(pg_settings) as connection:
        row = connection.execute(
            "SELECT * FROM videos WHERE id = ?",
            (video_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404)

    if row["source_type"] == "google_drive" and row["source_file_id"]:
        drive_client = GoogleDriveClient(get_google_drive_settings())

        # Open the stream from Google Drive
        resp = await drive_client.open_media_stream(row["source_file_id"], range_header=request.headers.get("range"))

        # Prepare headers for the browser
        headers = {
            "Accept-Ranges": "bytes",
        }
        if resp.headers.get("Content-Range"):
            headers["Content-Range"] = str(resp.headers.get("Content-Range"))

        # We DON'T pass Content-Length here to avoid mismatch errors if the stream closes early
        # or if there's any discrepancy. Browsers will handle chunked or unknown length for video tags.

        async def stream_from_resp(r):
            try:
                # Use smaller chunks for better reactivity
                async for chunk in r.aiter_bytes(chunk_size=256 * 1024):
                    yield chunk
            except Exception as e:
                logger.error(f"Streaming error for video {video_id}: {e}")
            finally:
                await r.aclose()
                # Close the associated client
                if hasattr(r, "_client"):
                    await r._client.aclose()

        return StreamingResponse(
            stream_from_resp(resp),
            status_code=resp.status_code,
            media_type=row["mime_type"] or "video/mp4",
            headers=headers,
        )

    if not row["local_video_path"]:
        raise HTTPException(status_code=404)
    return FileResponse(row["local_video_path"], filename=row["title"], media_type=row["mime_type"] or "video/mp4")
