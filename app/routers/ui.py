from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_access_token
from app.config import get_app_settings
from app.core import templates
from app.dependencies import get_search_service, get_settings
from app.services.search import SearchService
from app.settings import Settings

logger = logging.getLogger("app.routers.ui")

router = APIRouter(tags=["UI Pages"])


@router.get("/", response_class=HTMLResponse)
async def index_page(
    request: Request,
    q: str | None = None,
    mode: str = "hybrid",
    date_from: str | None = None,
    date_to: str | None = None,
    video_type: str = "all",
    search_service: SearchService = Depends(get_search_service),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Renders the main video indexing search engine home page."""
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
        try:
            results = await search_service.search(
                query=q or "",
                limit=settings.app_results_limit,
                search_mode=mode,
                date_from=date_from,
                date_to=date_to,
                video_type=video_type,
            )
        except Exception as e:
            logger.error(f"Search failed: {e}")
            results = []

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
    try:
        current_token = require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token:
        return RedirectResponse(url="/")

    return templates.TemplateResponse(request, "status.html", {})


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

    from app.manticore import get_manticore_client

    m_client = get_manticore_client()
    try:
        # Run scroll synchronously in thread pool
        res = await asyncio.to_thread(m_client.scroll, "speaker_registry", limit=100)
        speakers = []
        if res and res[0]:
            for p in res[0]:
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
