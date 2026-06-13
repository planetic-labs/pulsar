from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_access_token
from app.config import get_app_settings, get_sqlite_settings
from app.core import templates
from app.db import db_connection
from app.manticore import get_manticore_client
from app.schemas import VideoStatusItem
from app.search import hybrid_search

logger = logging.getLogger(__name__)

router = APIRouter(tags=["UI Pages"])


def _status_rows(connection: Any) -> list[VideoStatusItem]:
    """Helper to retrieve list of videos and their processing metadata status."""
    rows = connection.execute(
        """
        SELECT
            v.id, v.title, v.source_file_id, v.status AS processing_status, v.updated_at, v.created_at,
            CASE WHEN EXISTS(SELECT 1 FROM chunks c WHERE c.video_id = v.id) THEN 1 ELSE 0 END AS transcript_count,
            (SELECT COUNT(*) FROM chunks c WHERE c.video_id = v.id) AS chunk_count,
            'Deepgram' as primary_engine
        FROM videos v
        ORDER BY v.created_at DESC, v.id DESC
        """
    ).fetchall()
    return [VideoStatusItem(**dict(row)) for row in rows]


@router.get("/", response_class=HTMLResponse)
async def index_page(
    request: Request,
    q: str | None = None,
    mode: str = "hybrid",
    date_from: str | None = None,
    date_to: str | None = None,
    video_type: str = "all",
) -> Response:
    """Renders the main video indexing search engine home page."""
    app_settings = get_app_settings()
    pg_settings = get_sqlite_settings()

    # Debug log for request loops
    logger.info(
        "INDEX_PAGE: Path=%s, Query=%s, UA=%s",
        request.url.path,
        request.url.query,
        request.headers.get("user-agent"),
    )

    try:
        current_token = require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    results: list[Any] = []
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

    today_val = date.today().isoformat()
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
            "today_val": today_val,
            "default_start": default_start,
            "video_type": video_type,
            "token": current_token,
        },
    )


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request) -> Response:
    """Renders the Google Drive file import dashboard view."""
    try:
        current_token = require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token:
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "import.html", {})


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> Response:
    """Renders the queue monitor and task logs dashboard view."""
    pg_settings = get_sqlite_settings()

    try:
        current_token = require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token:
        return RedirectResponse(url="/")

    import json

    with db_connection(pg_settings) as connection:
        statuses = _status_rows(connection)

        # Helper to process task rows
        def process_tasks(rows: list[Any]) -> list[dict[str, Any]]:
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


@router.get("/speakers", response_class=HTMLResponse)
async def speakers_page(request: Request) -> Response:
    """Renders the speakers voice models manager dashboard view."""
    try:
        current_token = require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token:
        return RedirectResponse(url="/")

    m_client = get_manticore_client()
    try:
        res = m_client.scroll(collection_name="speaker_registry", limit=100)[0]
        speakers = []
        for p in res:
            if p.payload:
                speakers.append(
                    {
                        "id": p.id,
                        "name": p.payload.get("name", "Unknown"),
                        "audio_url": None,
                    }
                )
        return templates.TemplateResponse(request, "speakers.html", {"speakers": speakers})
    except Exception as e:
        logger.error(f"Error fetching speakers: {e}")
        return templates.TemplateResponse(request, "speakers.html", {"speakers": [], "error": str(e)})


@router.get("/indexed", response_class=HTMLResponse)
def indexed_page(request: Request) -> Response:
    """Renders the local indexed files and directory structure explorer view."""
    try:
        current_token = require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token:
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "indexed.html", {})
