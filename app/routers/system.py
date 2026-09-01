from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Response, WebSocket, WebSocketDisconnect, status

from app.auth import is_valid_token
from app.chunking import get_chunking_config_hash
from app.config import get_app_settings, get_embedding_settings, get_manticore_settings, get_sqlite_settings
from app.db import db_connection
from app.indexing_state import embedding_circuit_is_open
from app.manticore import get_manticore_client
from app.worker import broadcaster

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System & PWA"])


@router.websocket("/api/v1/logs/stream")
async def websocket_logs(websocket: WebSocket) -> None:
    """Streams live application console logs via WebSocket connection."""
    # Auth check: only admin is allowed to stream logs
    from app.config import get_app_settings

    settings = get_app_settings()
    token = websocket.session.get("access_token") or websocket.session.get("token")
    if not is_valid_token(token) or token != settings.access_token:
        # Check cookie as fallback
        token = websocket.cookies.get("access_token")
        if not is_valid_token(token) or token != settings.access_token:
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


def _manticore_count() -> int:
    settings = get_manticore_settings()
    response = get_manticore_client()._execute_sql(f"SELECT COUNT(*) AS count FROM `{settings.table_name}`")
    if not response or not response[0].get("data"):
        raise RuntimeError("Manticore returned no count row")
    row = response[0]["data"][0]
    if isinstance(row, dict):
        return int(row.get("count(*)", row.get("count", 0)))
    return int(row[0])


def _reliability_report() -> dict[str, Any]:
    app_settings = get_app_settings()
    embedding = get_embedding_settings()
    report: dict[str, Any] = {"checks": {}, "metrics": {}}
    try:
        with db_connection(get_sqlite_settings()) as conn:
            quick_check = str(conn.execute("PRAGMA quick_check(1)").fetchone()[0])
            sqlite_chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            generation = conn.execute(
                """
                SELECT id, name, status, config_hash, embedding_model, embedding_dimension,
                       expected_chunks, indexed_chunks, manticore_table
                FROM index_generations WHERE status = 'active' ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            task_counts = {
                str(row["status"]): int(row["cnt"])
                for row in conn.execute("SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status")
            }
            outbox_pending = int(
                conn.execute(
                    "SELECT COUNT(*) FROM index_outbox WHERE status IN ('pending', 'processing', 'failed')"
                ).fetchone()[0]
            )
            circuit_open = embedding_circuit_is_open(conn)
        report["checks"]["sqlite"] = {"ok": quick_check == "ok", "result": quick_check}
        report["checks"]["generation"] = {
            "ok": bool(
                generation
                and generation["config_hash"] == get_chunking_config_hash()
                and generation["embedding_model"] == embedding.model_id
                and int(generation["embedding_dimension"]) == embedding.dimension
            ),
            "active": dict(generation) if generation else None,
        }
        report["checks"]["embedding_circuit"] = {"ok": not circuit_open, "open": circuit_open}
        report["metrics"].update(
            {
                "sqlite_chunks": sqlite_chunks,
                "tasks_by_status": task_counts,
                "outbox_unfinished": outbox_pending,
            }
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        report["checks"]["sqlite"] = {"ok": False, "error": str(exc)}
        sqlite_chunks = -1

    marker = app_settings.data_dir / "REINDEX_REQUIRED"
    report["checks"]["restore_reindex"] = {"ok": not marker.exists(), "marker": str(marker)}
    try:
        manticore_chunks = _manticore_count()
        report["metrics"]["manticore_chunks"] = manticore_chunks
        report["checks"]["index_coverage"] = {
            "ok": sqlite_chunks >= 0 and sqlite_chunks == manticore_chunks,
            "sqlite": sqlite_chunks,
            "manticore": manticore_chunks,
        }
    except Exception as exc:
        report["checks"]["manticore"] = {"ok": False, "error": str(exc)}

    report["status"] = "ready" if all(check.get("ok", False) for check in report["checks"].values()) else "not_ready"
    return report


@router.get("/ready")
def readiness(response: Response) -> dict[str, Any]:
    """Report whether SQLite and the derived search index are safe to serve."""
    report = _reliability_report()
    if report["status"] != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report


@router.get("/api/v1/metrics/reliability")
def reliability_metrics() -> dict[str, Any]:
    """Expose queue, outbox, generation, and index coverage metrics as JSON."""
    return _reliability_report()


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
"""
    return Response(content=content, media_type="application/javascript")
