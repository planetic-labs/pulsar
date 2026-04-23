from __future__ import annotations

from pathlib import Path
import logging
import asyncio
import json

from fastapi import Depends, FastAPI, HTTPException, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import require_access_token, login_user, logout_user, get_session_token
from app.config import get_app_settings, get_google_drive_settings, get_sqlite_settings
from app.db import db_connection, init_db
from app.google_drive import GoogleDriveClient
from app.schemas import SearchResponse, SearchResultItem, VideoStatusItem
from app.search import hybrid_search
from app.worker import get_worker, logs_queue

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))
app = FastAPI(title="VideoDB")

# Session Middleware for Auth
app.add_middleware(SessionMiddleware, secret_key="super-secret-key")

def _status_rows(connection) -> list[VideoStatusItem]:
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

@app.on_event("startup")
async def on_startup() -> None:
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        init_db(connection)
    
    from app.qdrant import init_qdrant
    init_qdrant()
    
    # Enable thread-safe logging to WebSocket
    from app.worker import get_worker, set_main_loop
    set_main_loop(asyncio.get_running_loop())
    
    # Start background worker
    worker = get_worker()
    asyncio.create_task(worker.run())
    
    logger.info("Application initialized with background worker.")

@app.websocket("/api/v1/logs/stream")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    logger.info(f"WebSocket client connected from {websocket.client}")
    try:
        while True:
            # Get log from queue
            log_msg = await logs_queue.get()
            await websocket.send_text(log_msg)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")

@app.get("/api/v1/logs/poll")
async def api_poll_logs():
    """Fallback endpoint for environments where WebSockets are blocked."""
    msgs = []
    # Collect all currently available messages without blocking
    try:
        while not logs_queue.empty():
            msgs.append(logs_queue.get_nowait())
    except Exception:
        pass
    return {"logs": msgs}

@app.get("/api/v1/logs/stream")
async def websocket_logs_get():
    logger.info("HTTP GET request to log stream endpoint")
    return {"status": "log_stream_active", "transport": "websocket_required", "fallback_available": "/api/v1/logs/poll"}

from fastapi.staticfiles import StaticFiles
from app.qdrant import get_qdrant_client
from qdrant_client import models
import uuid
import subprocess
import imageio_ffmpeg

# ... (после инициализации app)
app.mount("/audio", StaticFiles(directory="/srv/search-ui/storage/voice_samples"), name="voice_audio")

# --- SPEAKERS ROUTES ---

@app.get("/speakers", response_class=HTMLResponse)
async def speakers_page(request: Request, _: str = Depends(require_access_token)):
    q_client = get_qdrant_client()
    try:
        res = q_client.scroll(collection_name="speaker_registry", limit=100)[0]
        speakers = []
        for p in res:
            speakers.append({
                "id": p.id,
                "name": p.payload.get("name", "Unknown"),
                "audio_url": f"/audio/{p.payload.get('sample_file')}" if p.payload.get("sample_file") else None
            })
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
    _: str = Depends(require_access_token)
):
    from app.voice import extract_speaker_embedding
    settings = get_sqlite_settings()
    
    # 1. Получаем путь к видео
    with db_connection(settings) as conn:
        video = conn.execute("SELECT local_audio_path FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not video or not video["local_audio_path"]:
            raise HTTPException(status_code=404, detail="Audio file not found for this video")
        audio_path = Path(video["local_audio_path"])

    # 2. Извлекаем эмбеддинг
    embedding = extract_speaker_embedding(audio_path, start_sec, end_sec)
    if not embedding:
        raise HTTPException(status_code=500, detail="Failed to extract voice embedding")

    # 3. Вырезаем и сохраняем аудио-фрагмент
    sample_id = str(uuid.uuid4())
    filename = f"{sample_id}.wav"
    save_path = Path("/srv/search-ui/storage/voice_samples") / filename
    
    try:
        duration = end_sec - start_sec
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_path, "-y", "-loglevel", "error",
            "-ss", str(start_sec),
            "-i", str(audio_path),
            "-t", str(duration),
            "-ar", "16000",
            "-ac", "1",
            str(save_path)
        ]
        subprocess.run(cmd, check=True)
    except Exception as e:
        logger.error(f"Failed to save audio sample: {e}")
        # Продолжаем без файла, если эмбеддинг есть

    # 4. Сохраняем в Qdrant
    q_client = get_qdrant_client()
    q_client.upsert(
        collection_name="speaker_registry",
        points=[
            models.PointStruct(
                id=sample_id,
                vector=embedding,
                payload={
                    "name": name,
                    "sample_file": filename if save_path.exists() else None,
                    "source_video_id": video_id,
                    "source_start": start_sec
                }
            )
        ]
    )
    
    return {"status": "success", "id": sample_id, "name": name}

@app.delete("/api/speakers/{speaker_id}")
async def api_delete_speaker(speaker_id: str, _: str = Depends(require_access_token)):
    q_client = get_qdrant_client()
    
    # Пытаемся найти инфу о файле перед удалением
    try:
        points = q_client.retrieve(collection_name="speaker_registry", ids=[speaker_id])
        if points:
            filename = points[0].payload.get("sample_file")
            if filename:
                file_path = Path("/srv/search-ui/storage/voice_samples") / filename
                if file_path.exists():
                    file_path.unlink()
    except Exception as e:
        logger.warning(f"Could not delete audio file for speaker {speaker_id}: {e}")

    q_client.delete(
        collection_name="speaker_registry",
        points_selector=models.PointIdsList(points=[speaker_id])
    )
    return {"status": "deleted"}

@app.post("/api/tasks/ingest")
async def api_add_ingest_task(
    file_id: str = Form(...),
    diarize: bool = Form(True),
    _: str = Depends(require_access_token)
):
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        conn.execute(
            "INSERT INTO tasks (task_type, payload) VALUES (?, ?)",
            ("ingest_video", json.dumps({"file_id": file_id, "diarize": diarize}))
        )
    return {"status": "queued", "file_id": file_id}

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
        login_user(response, request, token_param)
        return response

    current_token = get_session_token(request)
    if current_token != app_settings.access_token:
        return RedirectResponse(url="/login")

    results = []
    if q:
        with db_connection(pg_settings) as connection:
            items = hybrid_search(connection, q, limit=app_settings.results_limit)
            results = items
            if results:
                logger.info(f"DEBUG: First result chunk_id={results[0].chunk_id}, fields={dir(results[0])}")

    return templates.TemplateResponse(
        request, "index.html", {"query": q or "", "results": results}
    )

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

    return templates.TemplateResponse(request, "status.html", {"statuses": statuses})

@app.get("/api/videos/{video_id}/speakers")
def api_list_speakers(
    video_id: int,
    _: str = Depends(require_access_token)
):
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        # Find all unique speaker tags in chunks
        rows = conn.execute(
            "SELECT DISTINCT speaker_tags FROM chunks WHERE video_id = ?",
            (video_id,)
        ).fetchall()
        
        tags = set()
        for r in rows:
            if r["speaker_tags"]:
                for t in r["speaker_tags"].split(", "):
                    tags.add(t)
        
        # If no tags detected (old video or single speaker), provide a default tag 'primary'
        if not tags:
            tags.add("primary")
        
        # Get existing names from speakers table
        names_rows = conn.execute(
            "SELECT speaker_tag, name FROM speakers WHERE video_id = ?",
            (video_id,)
        ).fetchall()
        name_map = {r["speaker_tag"]: r["name"] for r in names_rows}
        
        return [{"tag": t, "name": name_map.get(t, f"Speaker {t}" if t != "primary" else "Основной голос")} for t in sorted(list(tags))]

@app.post("/api/videos/{video_id}/speakers")
async def api_save_speaker(
    video_id: int,
    request: Request,
    _: str = Depends(require_access_token)
):
    data = await request.json()
    tag = data.get("tag")
    name = data.get("name")
    
    if not tag or not name:
        raise HTTPException(status_code=400, detail="Missing tag or name")
        
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        # 1. Save to local SQLite
        conn.execute(
            """
            INSERT INTO speakers (video_id, speaker_tag, name) 
            VALUES (?, ?, ?)
            ON CONFLICT(video_id, speaker_tag) DO UPDATE SET name = EXCLUDED.name
            """,
            (video_id, str(tag), name)
        )
        
        # 2. Global Enrollment (Voice Fingerprinting)
        if tag == "primary":
            chunk = conn.execute(
                "SELECT start_sec, end_sec FROM chunks WHERE video_id = ? ORDER BY (end_sec - start_sec) DESC LIMIT 1",
                (video_id,)
            ).fetchone()
        else:
            chunk = conn.execute(
                "SELECT start_sec, end_sec FROM chunks WHERE video_id = ? AND speaker_tags LIKE ? ORDER BY (end_sec - start_sec) DESC LIMIT 1",
                (video_id, f"%{tag}%")
            ).fetchone()
        
        video = conn.execute("SELECT local_audio_path FROM videos WHERE id = ?", (video_id,)).fetchone()
        
        if chunk and video and video["local_audio_path"]:
            from app.voice import extract_speaker_embedding
            from app.qdrant import get_qdrant_client
            from qdrant_client import models
            import uuid
            import subprocess
            import imageio_ffmpeg
            
            # Use centered 20s window for better quality
            chunk_start = chunk["start_sec"]
            chunk_end = chunk["end_sec"]
            chunk_duration = chunk_end - chunk_start
            
            if chunk_duration > 20.0:
                mid = chunk_start + (chunk_duration / 2)
                start = max(chunk_start, mid - 10.0)
                end = min(chunk_end, start + 20.0)
            else:
                start = chunk_start
                end = chunk_end
            
            audio_path = Path(video["local_audio_path"])
            emb = extract_speaker_embedding(audio_path, start, end)
            
            if emb:
                sample_id = str(uuid.uuid4())
                filename = f"{sample_id}.wav"
                save_path = Path("/srv/search-ui/storage/voice_samples") / filename
                
                # Save audio segment
                try:
                    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
                    subprocess.run([
                        ffmpeg_path, "-y", "-loglevel", "error",
                        "-ss", str(start),
                        "-i", str(audio_path),
                        "-t", str(end - start),
                        "-ar", "16000", "-ac", "1",
                        str(save_path)
                    ], check=True)
                except Exception as e:
                    logger.error(f"Failed to save audio sample in api_save_speaker: {e}")

                qdrant = get_qdrant_client()
                qdrant.upsert(
                    collection_name="speaker_registry",
                    points=[
                        models.PointStruct(
                            id=sample_id,
                            vector=emb,
                            payload={
                                "name": name,
                                "sample_file": filename if save_path.exists() else None,
                                "source_video_id": video_id,
                                "source_start": start
                            }
                        )
                    ]
                )
    
    return {"status": "saved", "enrolled": True}

@app.get("/api/videos/{video_id}/export")
def api_video_export(
    video_id: int,
    _: str = Depends(require_access_token)
):
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT c.text, c.speaker_tags 
            FROM chunks c
            WHERE c.video_id = ?
            ORDER BY c.chunk_index ASC
            """,
            (video_id,)
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
        
        return Response(content=full_text, media_type="text/plain", headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.get("/api/videos/{video_id}/chunks")
def api_video_chunks(
    video_id: int,
    _: str = Depends(require_access_token)
):
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
            (video_id,)
        ).fetchall()
        return [dict(row) for row in rows]

@app.get("/api/videos")
def api_list_videos(_: str = Depends(require_access_token)):
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        rows = connection.execute("SELECT id, title FROM videos ORDER BY title ASC").fetchall()
        return [dict(row) for row in rows]

@app.post("/api/chunks/{chunk_id}")
async def api_update_chunk(
    chunk_id: int,
    request: Request,
    _: str = Depends(require_access_token)
):
    data = await request.json()
    new_text = data.get("text")
    if new_text is None:
        raise HTTPException(status_code=400, detail="Missing text")
    
    settings = get_sqlite_settings()
    from app.gemini import UnifiedEmbeddingClient
    from app.qdrant import get_qdrant_client, get_sparse_embedding_model
    from app.config import get_qdrant_settings, get_embedding_settings
    from qdrant_client import models
    
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
            (chunk_id,)
        ).fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Chunk not found")
        
        # 2. Обновляем PostgreSQL (только текст, векторы ушли в Qdrant)
        connection.execute("UPDATE chunks SET text = ? WHERE id = ?", (new_text, chunk_id))

        # 3. Генерируем НОВЫЕ векторы
        try:
            dense_vec, sparse_vec = embed_client.embed_text(new_text, task_type="RETRIEVAL_DOCUMENT")
            
            # 4. Обновляем Qdrant
            qdrant.upsert(
                collection_name=q_settings.collection_name,
                points=[
                    models.PointStruct(
                        id=chunk_id,
                        vector={
                            "default": dense_vec,
                            "text-sparse": sparse_vec
                        },
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
                            "is_primary": True
                        }
                    )
                ]
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
                    import json
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
async def api_drive_ls(
    folder_id: str | None = None,
    _: str = Depends(require_access_token)
):
    drive_client = GoogleDriveClient(get_google_drive_settings())
    target_id = folder_id or "root"
    
    try:
        items = drive_client.list_folder_contents(target_id)
        return items
    except Exception as e:
        # If it's an auth error, we return a specific hint for the UI
        if "token" in str(e).lower() or "auth" in str(e).lower() or "400" in str(e).lower():
            raise HTTPException(status_code=401, detail="Google Drive re-authentication required")
        import traceback
        logger.error(f"Error in drive_ls for folder {target_id}: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/drive/auth/init")
async def api_drive_auth_init(_: str = Depends(require_access_token)):
    drive_client = GoogleDriveClient(get_google_drive_settings())
    auth_url, _ = drive_client.auth_init()
    return {"auth_url": auth_url}

@app.get("/api/drive/auth/callback")
async def api_drive_auth_callback(request: Request):
    # This is the endpoint Google redirects back to
    drive_client = GoogleDriveClient(get_google_drive_settings())
    try:
        full_url = str(request.url)
        drive_client.auth_exchange(full_url)
        return HTMLResponse("<html><body><script>window.close();</script>Авторизация успешна! Это окно можно закрыть.</body></html>")
    except Exception as e:
        return HTMLResponse(f"<html><body>Ошибка авторизации: {e}</body></html>")

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
            "SELECT local_video_path, title, mime_type, source_type, source_file_id FROM videos WHERE id = ?",
            (video_id,),
        ).fetchone()
    
    if not row: raise HTTPException(status_code=404)

    if row["source_type"] == "google_drive" and row["source_file_id"]:
        drive_client = GoogleDriveClient(get_google_drive_settings())
        drive_response = drive_client.open_media_stream(row["source_file_id"], range_header=request.headers.get("range"))
        if drive_response:
            passthrough_headers = {h: drive_response.headers.get(h) for h in ("Content-Range", "Accept-Ranges", "Content-Length") if drive_response.headers.get(h)}
            return StreamingResponse(
                (chunk for chunk in iter(lambda: drive_response.read(1024*1024), b'')),
                status_code=getattr(drive_response, "status", 200),
                media_type=row["mime_type"] or "video/mp4",
                headers=passthrough_headers
            )

    if not row["local_video_path"]: raise HTTPException(status_code=404)
    return FileResponse(row["local_video_path"], filename=row["title"], media_type=row["mime_type"] or "video/mp4")
