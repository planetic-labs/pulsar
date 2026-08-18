from __future__ import annotations

import asyncio
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import get_authenticated_user_id, require_access_token, require_subeditor
from app.config import get_app_settings
from app.core import templates
from app.dependencies import get_chunk_repo, get_search_history_repo, get_search_service, get_settings
from app.limiter import limiter
from app.repos.chunk_repo import ChunkRepository
from app.repos.search_history_repo import SearchHistoryRepository
from app.services.search import SearchResult, SearchService
from app.settings import Settings

logger = logging.getLogger("app.routers.ui")

router = APIRouter(tags=["UI Pages"])


def is_mobile_request(request: Request) -> bool:
    ua = request.headers.get("user-agent", "").lower()
    return any(m in ua for m in ["mobile", "android", "iphone", "ipad"])


@router.get("/", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def index_page(
    request: Request,
    q: str | None = None,
    mode: str = "hybrid",
    date_from: str | None = None,
    date_to: str | None = None,
    video_type: str = "all",
    search_service: SearchService = Depends(get_search_service),
    search_history_repo: SearchHistoryRepository = Depends(get_search_history_repo),
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
        current_token = await require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    user_id = get_authenticated_user_id(request, current_token)
    search_history: list[str] = []
    if user_id:
        if q and q.strip():
            try:
                await search_history_repo.add_query(user_id, q.strip())
            except Exception as e:
                logger.warning(f"Failed to record search history: {e}")

        try:
            search_history = await search_history_repo.get_history(user_id, limit=10)
        except Exception as e:
            logger.warning(f"Failed to fetch search history: {e}")

    results: list[SearchResult] = []
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

    is_mobile = is_mobile_request(request)
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
            "search_history": search_history,
        },
    )


@router.get("/api/search/history")
async def api_get_search_history(
    request: Request,
    limit: int = 10,
    search_history_repo: SearchHistoryRepository = Depends(get_search_history_repo),
    token: str = Depends(require_access_token),
) -> dict[str, list[str]]:
    """Returns list of recent search queries for personal user accounts only."""
    user_id = get_authenticated_user_id(request, token)
    if not user_id:
        return {"history": []}
    history = await search_history_repo.get_history(user_id, limit=limit)
    return {"history": history}


@router.delete("/api/search/history")
async def api_delete_search_history(
    request: Request,
    q: str | None = None,
    search_history_repo: SearchHistoryRepository = Depends(get_search_history_repo),
    token: str = Depends(require_access_token),
) -> dict[str, str]:
    """Deletes a specific query or clears entire search history for personal user accounts only."""
    user_id = get_authenticated_user_id(request, token)
    if user_id:
        if q:
            await search_history_repo.delete_query(user_id, q)
        else:
            await search_history_repo.clear_history(user_id)
    return {"status": "ok"}


@router.get("/import", response_class=HTMLResponse)
async def import_page(request: Request) -> Response:
    """Renders the Google Drive file import dashboard view."""
    if is_mobile_request(request):
        return RedirectResponse(url="/")

    try:
        current_token = await require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token and settings.admin_role_name not in getattr(request.state, "roles", []):
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "import.html", {})


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request) -> Response:
    """Renders the queue monitor and task logs dashboard view."""
    if is_mobile_request(request):
        return RedirectResponse(url="/")

    try:
        current_token = await require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token and settings.admin_role_name not in getattr(request.state, "roles", []):
        return RedirectResponse(url="/")

    return templates.TemplateResponse(request, "status.html", {})


@router.get("/speakers", response_class=HTMLResponse)
async def speakers_page(request: Request) -> Response:
    """Renders the speakers voice models manager dashboard view."""
    if is_mobile_request(request):
        return RedirectResponse(url="/")

    try:
        current_token = await require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token and settings.admin_role_name not in getattr(request.state, "roles", []):
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
async def indexed_page(request: Request) -> Response:
    """Renders the local indexed files and directory structure explorer view."""
    if is_mobile_request(request):
        return RedirectResponse(url="/")

    try:
        current_token = await require_access_token(request)
    except HTTPException:
        return RedirectResponse(url="/login")

    settings = get_app_settings()
    if current_token != settings.access_token and settings.admin_role_name not in getattr(request.state, "roles", []):
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "indexed.html", {})


@router.get("/moderation", response_class=HTMLResponse)
async def moderation_page(
    request: Request,
    chunk_repo: ChunkRepository = Depends(get_chunk_repo),
) -> Response:
    """Renders the subtitle moderation/editing queue dashboard view."""
    if is_mobile_request(request):
        return RedirectResponse(url="/")

    try:
        await require_subeditor(request)
    except HTTPException:
        return RedirectResponse(url="/")

    from app.config import get_app_settings
    from app.services.search import format_timestamp

    user_id = getattr(request.state, "user_id", "anonymous")
    settings = get_app_settings()

    flag = await chunk_repo.reserve_and_get_next_flag(
        user_id=user_id, lock_timeout_sec=settings.subtitle_lock_timeout_sec
    )
    flags = [flag] if flag else []

    return templates.TemplateResponse(
        request, "moderation.html", {"flags": flags, "format_timestamp": format_timestamp}
    )
