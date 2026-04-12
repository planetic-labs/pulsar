from __future__ import annotations

from pathlib import Path
import logging

from fastapi import Depends, FastAPI, HTTPException, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import require_access_token, login_user, logout_user, get_session_token
from app.config import get_app_settings, get_google_drive_settings, get_postgres_settings
from app.db import db_connection, init_db
from app.google_drive import GoogleDriveClient
from app.schemas import SearchResponse, SearchResultItem, VideoStatusItem
from app.search import hybrid_search

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
            v.local_video_path, v.local_audio_path, v.updated_at,
            (SELECT COUNT(*) FROM transcripts t WHERE t.video_id = v.id) AS transcript_count,
            (SELECT COUNT(*) FROM chunks c WHERE c.video_id = v.id) AS chunk_count
        FROM videos v
        ORDER BY v.updated_at DESC, v.id DESC
        """
    ).fetchall()
    return [VideoStatusItem(**dict(row)) for row in rows]

@app.on_event("startup")
def on_startup() -> None:
    settings = get_postgres_settings()
    with db_connection(settings) as connection:
        init_db(connection)
    logger.info("Application initialized.")

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
    pg_settings = get_postgres_settings()
    
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

    return templates.TemplateResponse(
        request, "index.html", {"query": q or "", "results": results}
    )

@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    app_settings = get_app_settings()
    pg_settings = get_postgres_settings()
    
    current_token = get_session_token(request)
    if current_token != app_settings.access_token:
        return RedirectResponse(url="/login")

    with db_connection(pg_settings) as connection:
        statuses = _status_rows(connection)

    return templates.TemplateResponse(request, "status.html", {"statuses": statuses})

@app.get("/api/videos/{video_id}/alternatives")
def api_video_alternatives(
    video_id: int,
    start_sec: float,
    _: str = Depends(require_access_token)
):
    settings = get_postgres_settings()
    from app.search import get_alternative_transcripts
    with db_connection(settings) as connection:
        return get_alternative_transcripts(connection, video_id, start_sec)

@app.get("/api/videos/{video_id}/export")
def api_video_export(
    video_id: int,
    engine: str,
    _: str = Depends(require_access_token)
):
    settings = get_postgres_settings()
    with db_connection(settings) as connection:
        # Get all chunks for this transcript ordered by index
        rows = connection.execute(
            """
            SELECT c.text 
            FROM chunks c
            JOIN transcripts t ON t.id = c.transcript_id
            WHERE c.video_id = %s AND t.engine = %s
            ORDER BY c.chunk_index ASC
            """,
            (video_id, engine)
        ).fetchall()
        
        if not rows:
            raise HTTPException(status_code=404, detail="Transcript not found")
        
        full_text = "\n\n".join(row["text"] for row in rows)
        
        # Get video title for filename
        v_row = connection.execute("SELECT title FROM videos WHERE id = %s", (video_id,)).fetchone()
        filename = f"{v_row['title']}_{engine}.txt" if v_row else f"transcript_{video_id}_{engine}.txt"
        
        return Response(
            content=full_text,
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

@app.get("/api/videos/{video_id}/chunks")
def api_video_chunks(
    video_id: int,
    _: str = Depends(require_access_token)
):
    settings = get_postgres_settings()
    with db_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT c.id, c.start_sec, c.end_sec, c.text 
            FROM chunks c
            JOIN transcripts t ON t.id = c.transcript_id
            WHERE c.video_id = %s AND t.is_primary = TRUE
            ORDER BY c.chunk_index ASC
            """,
            (video_id,)
        ).fetchall()
        return [dict(row) for row in rows]

@app.get("/api/videos")
def api_list_videos(_: str = Depends(require_access_token)):
    settings = get_postgres_settings()
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
    
    settings = get_postgres_settings()
    from app.search import GoogleEmbeddingClient
    embed_client = GoogleEmbeddingClient()

    with db_connection(settings) as connection:
        # 1. Получаем информацию о чанке
        row = connection.execute(
            """
            SELECT c.chunk_index, t.normalized_json_path 
            FROM chunks c
            JOIN transcripts t ON t.id = c.transcript_id
            WHERE c.id = %s
            """,
            (chunk_id,)
        ).fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Chunk not found")
        
        chunk_index = row["chunk_index"]
        json_path_str = row["normalized_json_path"]

        # 2. Генерируем НОВЫЙ вектор для исправленного текста
        try:
            new_embedding = embed_client.embed_text(new_text, is_query=False)
        except Exception as e:
            logger.error(f"Failed to generate embedding for chunk {chunk_id}: {e}")
            new_embedding = None

        # 3. Обновляем базу данных (текст + вектор)
        if new_embedding:
            connection.execute(
                "UPDATE chunks SET text = %s, embedding = %s WHERE id = %s",
                (new_text, new_embedding, chunk_id)
            )
        else:
            connection.execute(
                "UPDATE chunks SET text = %s WHERE id = %s",
                (new_text, chunk_id)
            )

        # 4. Обновляем физический файл на диске
        if json_path_str:
            path = Path(json_path_str)
            if path.exists():
                try:
                    import json
                    content = json.loads(path.read_text(encoding="utf-8"))
                    if "utterances" in content and len(content["utterances"]) > chunk_index:
                        content["utterances"][chunk_index]["text"] = new_text
                    elif "chunks" in content and len(content["chunks"]) > chunk_index:
                        content["chunks"][chunk_index]["text"] = new_text
                    
                    if "utterances" in content:
                        content["transcript"] = " ".join(u["text"] for u in content["utterances"])
                    
                    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as e:
                    logger.error(f"Failed to sync JSON file {path}: {e}")

    return {"status": "updated", "vector_updated": True if new_embedding else False}

@app.get("/editor", response_class=HTMLResponse)
def editor_page(request: Request):
    app_settings = get_app_settings()
    current_token = get_session_token(request)
    if current_token != app_settings.access_token:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "editor.html", {})

# --- API & STREAMING ---

@app.get("/videos/{video_id}/file")
def video_file(video_id: int, request: Request, token: str | None = None) -> Response:
    app_settings = get_app_settings()
    # Check token from session or query param
    current_token = get_session_token(request) or token
    if current_token != app_settings.access_token:
        raise HTTPException(status_code=401)

    pg_settings = get_postgres_settings()
    with db_connection(pg_settings) as connection:
        row = connection.execute(
            "SELECT local_video_path, title, mime_type, source_type, source_file_id FROM videos WHERE id = %s",
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
