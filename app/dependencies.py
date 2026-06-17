from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends

from app.adapters.deepgram import DeepgramAdapter
from app.adapters.embeddings import EmbeddingAdapter
from app.adapters.google_drive import GoogleDriveAdapter
from app.adapters.manticore import ManticoreAdapter
from app.config import get_deepgram_settings, get_embedding_settings, get_google_drive_settings
from app.database import Database
from app.ports import EmbeddingPort, FileStoragePort, TranscriptionPort, VectorStorePort
from app.repos.cache_repo import CacheRepository
from app.repos.chunk_repo import ChunkRepository
from app.repos.folder_repo import FolderRepository
from app.repos.task_repo import TaskRepository
from app.repos.video_repo import VideoRepository
from app.services.search import SearchService
from app.services.video import VideoService
from app.settings import Settings, get_settings


async def get_database(settings: Settings = Depends(get_settings)) -> AsyncGenerator[Database, None]:
    """Асинхронный генератор для предоставления экземпляра Database с управлением его жизненным циклом."""
    db = Database(settings.resolved_db_path)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


def get_manticore(settings: Settings = Depends(get_settings)) -> VectorStorePort:
    return ManticoreAdapter(settings.manticore_url)


def get_google_drive() -> FileStoragePort:
    return GoogleDriveAdapter(get_google_drive_settings())


def get_deepgram() -> TranscriptionPort:
    return DeepgramAdapter(get_deepgram_settings())


def get_embedder() -> EmbeddingPort:
    return EmbeddingAdapter(get_embedding_settings())


def get_video_repo(db: Database = Depends(get_database)) -> VideoRepository:
    return VideoRepository(db)


def get_chunk_repo(db: Database = Depends(get_database)) -> ChunkRepository:
    return ChunkRepository(db)


def get_task_repo(db: Database = Depends(get_database)) -> TaskRepository:
    return TaskRepository(db)


def get_folder_repo(db: Database = Depends(get_database)) -> FolderRepository:
    return FolderRepository(db)


def get_cache_repo(db: Database = Depends(get_database)) -> CacheRepository:
    return CacheRepository(db)


def get_video_service(
    db: Database = Depends(get_database),
    video_repo: VideoRepository = Depends(get_video_repo),
    chunk_repo: ChunkRepository = Depends(get_chunk_repo),
    task_repo: TaskRepository = Depends(get_task_repo),
    manticore: VectorStorePort = Depends(get_manticore),
    embedder: EmbeddingPort = Depends(get_embedder),
    settings: Settings = Depends(get_settings),
) -> VideoService:
    return VideoService(
        db=db,
        video_repo=video_repo,
        chunk_repo=chunk_repo,
        task_repo=task_repo,
        manticore=manticore,
        embedder=embedder,
        settings=settings,
    )


def get_search_service(
    embedder: EmbeddingPort = Depends(get_embedder),
    manticore: VectorStorePort = Depends(get_manticore),
    video_repo: VideoRepository = Depends(get_video_repo),
    cache_repo: CacheRepository = Depends(get_cache_repo),
    settings: Settings = Depends(get_settings),
) -> SearchService:
    return SearchService(
        embedder=embedder,
        vector_store=manticore,
        video_repo=video_repo,
        cache_repo=cache_repo,
        settings=settings,
    )
