from __future__ import annotations

import time
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import get_sqlite_settings
from app.db import db_connection

ROOT_DIR: Path = Path(__file__).resolve().parents[1]
templates: Jinja2Templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))

# Cache for global stats (60s)
_global_stats_cache: dict[str, dict[str, int] | None | float] = {"data": None, "timestamp": 0.0}


def get_global_stats() -> dict[str, int | bool]:
    """Fetches global statistics about indexed videos, duration, and worker status.

    Includes a 60-second caching mechanism.
    """
    global _global_stats_cache
    now: float = time.time()
    settings = get_sqlite_settings()
    worker_busy: bool = False
    try:
        with db_connection(settings) as conn:
            task_check = conn.execute("SELECT 1 FROM tasks WHERE status IN ('pending', 'running') LIMIT 1").fetchone()
            worker_busy = task_check is not None

            cached_timestamp = _global_stats_cache.get("timestamp", 0.0)
            cached_data = _global_stats_cache.get("data")
            if (
                isinstance(cached_timestamp, (int, float))
                and now - cached_timestamp < 60
                and isinstance(cached_data, dict)
            ):
                data = cached_data.copy()
                data["worker_busy"] = worker_busy
                return data

            sql = (
                "SELECT COUNT(*) as count, SUM(duration_sec) as total_sec "
                "FROM videos WHERE status = 'indexed_chunks_ready'"
            )
            row = conn.execute(sql).fetchone()
            total_sec = row["total_sec"] or 0
            count = row["count"]
            hours = int(total_sec // 3600)

            new_data = {"total_videos": count, "total_hours": hours}
            _global_stats_cache["data"] = new_data
            _global_stats_cache["timestamp"] = now

            data = new_data.copy()
            data["worker_busy"] = worker_busy
            return data
    except Exception:
        return {"total_videos": 0, "total_hours": 0, "worker_busy": False}
