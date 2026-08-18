from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import cast

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_app_settings
from app.core import ROOT_DIR, get_global_stats, templates
from app.database import Database
from app.limiter import limiter
from app.manticore import init_manticore
from app.routers import auth, indexed, system, ui, videos, worker
from app.settings import get_settings
from app.worker import get_worker, set_main_loop

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Startup logic
    settings = get_settings()
    db = Database(settings.resolved_db_path)
    await db.connect()
    await db.init_schema()
    await db.close()

    init_manticore()

    # Enable thread-safe logging to WebSocket
    set_main_loop(asyncio.get_running_loop())

    # Start background worker
    worker_instance = get_worker()
    worker_instance.start()

    logger.info("Application initialized with background worker.")

    yield

    # Shutdown logic
    logger.info("Shutting down...")
    worker_instance = get_worker()
    if worker_instance.is_running:
        worker_instance.stop()

    if worker_instance.task:
        try:
            logger.info("Waiting for background worker graceful shutdown...")
            await asyncio.wait_for(worker_instance.task, timeout=10.0)
        except TimeoutError:
            logger.warning("Graceful shutdown timed out, cancelling worker task.")
            worker_instance.task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_instance.task


app = FastAPI(title="Pulsar", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore # pyre-ignore


@app.middleware("http")
async def add_global_stats_to_templates(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # Manually inject global stats into templates environment globals

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
    settings.raw_transcripts_dir,
    settings.normalized_transcripts_dir,
]:
    d.mkdir(parents=True, exist_ok=True)

# Static files mapping
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")

# Register APIRouters
app.include_router(auth.router)
app.include_router(worker.router)
app.include_router(indexed.router)
app.include_router(videos.router)
app.include_router(ui.router)
app.include_router(system.router)
