from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from qdrant_client import models
from starlette.middleware.sessions import SessionMiddleware

from app.auth import get_session_token, login_user, logout_user, require_access_token
from app.config import (
    get_app_settings,
    get_embedding_settings,
    get_google_drive_settings,
    get_qdrant_settings,
    get_sqlite_settings,
)
from app.db import db_connection, init_db
from app.gemini import UnifiedEmbeddingClient
from app.google_drive import GoogleDriveClient
from app.qdrant import get_qdrant_client, init_qdrant
from app.schemas import VideoStatusItem
from app.search import hybrid_search
from app.worker import broadcaster, get_worker, set_main_loop

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))


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


app = FastAPI(title="VideoDB", lifespan=lifespan)

# Session Middleware for Auth
app.add_middleware(SessionMiddleware, secret_key="super-secret-key")

# Static files for voice samples
app.mount("/audio", StaticFiles(directory="/srv/search-ui/storage/voice_samples"), name="voice_audio")


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
async def speakers_page(request: Request, _: str = Depends(require_access_token)):
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
    _: str = Depends(require_access_token),
):
    # Logic to save voice samples and embeddings is disabled to save space and avoid 405 errors
    logger.info(f"Manual speaker registration for '{name}' received but ignored (storage disabled).")
    return {"status": "success", "info": "Global enrollment disabled for now."}


@app.delete("/api/speakers/{speaker_id}")
async def api_delete_speaker(speaker_id: str, _: str = Depends(require_access_token)):
    q_client = get_qdrant_client()

    # Пытаемся найти инфу о файле перед удалением
    try:
        points = q_client.retrieve(collection_name="speaker_registry", ids=[speaker_id])
        if points and points[0].payload:
            filename = points[0].payload.get("sample_file")
            if filename:
                file_path = Path("/srv/search-ui/storage/voice_samples") / filename
                if file_path.exists():
                    file_path.unlink()
    except Exception:
        pass

    q_client.delete(collection_name="speaker_registry", points_selector=models.PointIdsList(points=[speaker_id]))
    return {"status": "deleted"}


@app.post("/api/tasks/ingest")
async def api_add_ingest_task(
    file_id: str = Form(...), diarize: bool = Form(True), _: str = Depends(require_access_token)
):
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        conn.execute(
            "INSERT INTO tasks (task_type, payload) VALUES (?, ?)",
            ("stage_1_download", json.dumps({"file_id": file_id, "diarize": diarize})),
        )
    return {"status": "queued", "file_id": file_id}


@app.get("/indexed", response_class=HTMLResponse)
def indexed_page(request: Request):
    app_settings = get_app_settings()
    current_token = get_session_token(request)
    if current_token != app_settings.access_token:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "indexed.html", {})


@app.get("/api/v1/indexed/ls")
async def api_indexed_ls(folder_id: str | None = None, _: str = Depends(require_access_token)):
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
                v.id, v.title, v.mime_type, v.duration_sec, v.updated_at, v.source_file_id,
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
                "source_file_id": r["source_file_id"],
                "duration_sec": r["duration_sec"],
                "chunk_count": r["chunk_count"],
                "language": r["language"],
                "confidence": r["confidence"],
                "updated_at": r["updated_at"],
                "engine": "Deepgram",
            }
        )

    return {"items": items, "path": path}


@app.post("/api/v1/indexed/mkdir")
async def api_indexed_mkdir(
    name: str = Form(...), parent_id: str | None = Form(None), _: str = Depends(require_access_token)
):
    """Create a new folder in the internal hierarchy."""
    pg_settings = get_sqlite_settings()
    new_id = f"custom_{uuid.uuid4().hex[:12]}"
    real_parent = parent_id if parent_id and parent_id != "root" else None

    with db_connection(pg_settings) as conn:
        conn.execute("INSERT INTO folders (id, name, parent_id) VALUES (?, ?, ?)", (new_id, name, real_parent))
    return {"status": "success", "id": new_id}


@app.post("/api/v1/indexed/move")
async def api_indexed_move(
    video_id: int = Form(...), folder_id: str | None = Form(None), _: str = Depends(require_access_token)
):
    """Move a video to a specific folder."""
    pg_settings = get_sqlite_settings()
    real_target = folder_id if folder_id and folder_id != "root" else None

    with db_connection(pg_settings) as conn:
        conn.execute("UPDATE videos SET parent_folder_id = ? WHERE id = ?", (real_target, video_id))
    return {"status": "success"}


@app.post("/api/v1/indexed/folders/rename")
async def api_indexed_rename_folder(
    folder_id: str = Form(...), new_name: str = Form(...), _: str = Depends(require_access_token)
):
    """Rename a folder."""
    pg_settings = get_sqlite_settings()
    with db_connection(pg_settings) as conn:
        conn.execute("UPDATE folders SET name = ? WHERE id = ?", (new_name, folder_id))
    return {"status": "success"}


@app.delete("/api/v1/indexed/folders/{folder_id}")
async def api_indexed_delete_folder(folder_id: str, _: str = Depends(require_access_token)):
    """Delete a folder. Subfolders and videos will lose their parent reference."""
    pg_settings = get_sqlite_settings()
    with db_connection(pg_settings) as conn:
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    return {"status": "success"}


@app.post("/api/v1/indexed/sync")
async def api_indexed_sync(_: str = Depends(require_access_token)):
    """Trigger metadata synchronization for all indexed files."""
    from scripts.sync_titles import sync_indexed_metadata

    try:
        # Run in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        count = await loop.run_in_executor(None, sync_indexed_metadata)
        return {"status": "success", "updated_count": count}
    except Exception as e:
        logger.error(f"Metadata sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --- AUTH ROUTES ---


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login_post(request: Request, response: Response, token: str = Form(...)):
    if login_user(response, request, token):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid access token"})


@app.get("/logout")
def logout(request: Request, response: Response):
    logout_user(response, request)
    return RedirectResponse(url="/login")


# --- UI ROUTES ---


@app.get("/", response_class=HTMLResponse)
def index_page(request: Request, q: str | None = None):
    app_settings = get_app_settings()
    pg_settings = get_sqlite_settings()

    # URL Token Auth
    token_param = request.query_params.get("token")
    if token_param == app_settings.access_token:
        response = RedirectResponse(url=f"/?q={q or ''}")
        login_user(response, request, str(token_param))
        return response

    current_token = get_session_token(request)
    if current_token != app_settings.access_token:
        return RedirectResponse(url="/login")

    results = []
    if q:
        with db_connection(pg_settings) as connection:
            items = hybrid_search(connection, q, limit=app_settings.results_limit)
            results = items

    return templates.TemplateResponse(request, "index.html", {"query": q or "", "results": results})


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    app_settings = get_app_settings()
    current_token = get_session_token(request)
    if current_token != app_settings.access_token:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "import.html", {})


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    app_settings = get_app_settings()
    pg_settings = get_sqlite_settings()

    current_token = get_session_token(request)
    if current_token != app_settings.access_token:
        return RedirectResponse(url="/login")

    with db_connection(pg_settings) as connection:
        statuses = _status_rows(connection)

        # Fetch tasks (queue)
        sql_t = """
            SELECT * FROM tasks
            WHERE status IN ('pending', 'running', 'failed')
            ORDER BY created_at DESC LIMIT 50
        """
        task_rows = connection.execute(sql_t).fetchall()

        tasks = []
        for row in task_rows:
            t = dict(row)
            try:
                payload = json.loads(t["payload"])
                t["file_id"] = payload.get("file_id")
                # Try to find title if video row already created
                if t["file_id"]:
                    sql_v = "SELECT title FROM videos WHERE source_file_id = ?"
                    v = connection.execute(sql_v, (t["file_id"],)).fetchone()
                    t["title"] = v["title"] if v else f"Файл {t['file_id'][:8]}..."
                else:
                    t["title"] = payload.get("title", "AI Indexing")
            except Exception:
                t["title"] = "Task"
            tasks.append(t)

    return templates.TemplateResponse(request, "status.html", {"statuses": statuses, "tasks": tasks})


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
            dense_vec, sparse_vec = embed_client.embed_text(new_text, task_type="RETRIEVAL_DOCUMENT")

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
async def api_drive_ls(folder_id: str | None = None, _: str = Depends(require_access_token)):
    drive_client = GoogleDriveClient(get_google_drive_settings())
    target_id = folder_id or "root"

    try:
        items = drive_client.list_folder_contents(target_id)

        # Check database for indexed status
        sqlite_settings = get_sqlite_settings()
        with db_connection(sqlite_settings) as connection:
            # Get all source_file_ids for google_drive source
            indexed_rows = connection.execute(
                "SELECT source_file_id FROM videos WHERE source_type = 'google_drive'"
            ).fetchall()
            indexed_ids = {row["source_file_id"] for row in indexed_rows}

            # Also check if folders are indexed
            folder_rows = connection.execute("SELECT id FROM folders").fetchall()
            indexed_folder_ids = {row["id"] for row in folder_rows}

            # Check for tasks in queue
            queued_rows = connection.execute(
                "SELECT json_extract(payload, '$.file_id') as file_id, json_extract(payload, '$.video_id') as video_id FROM tasks WHERE status IN ('pending', 'running')"
            ).fetchall()
            
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
            item["is_indexed"] = item["id"] in (indexed_folder_ids if item.get("is_folder") else indexed_ids)
            item["is_queued"] = item["id"] in queued_ids

        return items
    except Exception as e:
        # If it's an auth error, we return a specific hint for the UI
        if "token" in str(e).lower() or "auth" in str(e).lower():
            raise HTTPException(status_code=401, detail="Google Drive re-authentication required") from e
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/drive/auth/init")
async def api_drive_auth_init(_token: str = Depends(require_access_token)):
    drive_client = GoogleDriveClient(get_google_drive_settings())
    auth_url, _ = drive_client.auth_init()
    return {"auth_url": auth_url}


@app.get("/api/drive/auth/callback")
async def api_drive_auth_callback(request: Request):
    # This is the endpoint Google redirects back to
    drive_client = GoogleDriveClient(get_google_drive_settings())
    try:
        drive_client.auth_exchange(str(request.url))
        return HTMLResponse("<html><body><script>window.close();</script>Авторизация успешна!</body></html>")
    except Exception as e:
        return HTMLResponse(f"<html><body>Ошибка: {e}</body></html>")


# --- API & STREAMING ---


@app.get("/videos/{video_id}/file")
def video_file(video_id: int, request: Request, token: str | None = None) -> Response:
    app_settings = get_app_settings()
    # Check token from session or query param
    current_token = get_session_token(request) or token
    if current_token != app_settings.access_token:
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
        drive_response = drive_client.open_media_stream(
            row["source_file_id"], range_header=request.headers.get("range")
        )
        if drive_response:
            headers = {
                h: str(drive_response.headers.get(h))
                for h in ("Content-Range", "Accept-Ranges", "Content-Length")
                if drive_response.headers.get(h)
            }
            return StreamingResponse(
                (chunk for chunk in iter(lambda: drive_response.read(1024 * 1024), b"")),
                status_code=getattr(drive_response, "status", 200),
                media_type=row["mime_type"] or "video/mp4",
                headers=headers,
            )

    if not row["local_video_path"]:
        raise HTTPException(status_code=404)
    return FileResponse(row["local_video_path"], filename=row["title"], media_type=row["mime_type"] or "video/mp4")
