from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_app_settings, get_sqlite_settings
from app.core import ROOT_DIR, get_global_stats, templates
from app.db import db_connection, init_db
from app.manticore import init_manticore
from app.routers import auth, indexed, system, ui, videos, worker
from app.worker import get_worker, set_main_loop

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        init_db(connection)

    init_manticore()

    # Enable thread-safe logging to WebSocket
    set_main_loop(asyncio.get_running_loop())

    # Start background worker
    worker_instance = get_worker()
    asyncio.create_task(worker_instance.run())

    logger.info("Application initialized with background worker.")

    yield

    # Shutdown logic (if any)
    logger.info("Shutting down...")


app = FastAPI(title="Pulsar", lifespan=lifespan)


@app.middleware("http")
async def add_global_stats_to_templates(request: Request, call_next):
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
