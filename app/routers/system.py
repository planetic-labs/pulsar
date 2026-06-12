from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Response, WebSocket, WebSocketDisconnect, status

from app.auth import is_valid_token
from app.worker import broadcaster

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System & PWA"])


@router.websocket("/api/v1/logs/stream")
async def websocket_logs(websocket: WebSocket) -> None:
    """Streams live application console logs via WebSocket connection."""
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


@router.get("/api/v1/logs/poll")
async def api_poll_logs() -> dict[str, str]:
    """Fallback endpoint for logs in environments where WebSockets are blocked."""
    return {"status": "polling_not_implemented", "hint": "Use WebSockets"}


@router.get("/api/v1/logs/stream")
async def websocket_logs_get() -> dict[str, str]:
    """Helper response suggesting using WebSocket protocol for log streams."""
    return {"status": "log_stream_active", "transport": "websocket_required"}


@router.get("/health")
def health() -> dict[str, str]:
    """Returns basic health status of the application."""
    return {"status": "ok"}


@router.get("/manifest.json")
async def manifest() -> dict[str, Any]:
    """Returns PWA application manifest metadata."""
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


@router.get("/sw.js")
async def service_worker() -> Response:
    """Returns PWA service worker pass-through script."""
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
