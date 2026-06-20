from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from app.database import Database
from app.manticore import models
from app.ports import ScoredPoint
from app.repos.cache_repo import CacheRepository
from app.repos.chunk_repo import ChunkRepository
from app.repos.task_repo import TaskRepository
from app.repos.video_repo import VideoRepository
from app.services.search import SearchService
from app.services.video import VideoService
from app.settings import Settings


class MockEmbeddingAdapter:
    async def embed_text(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        return [0.1] * 128, None

    async def embed_batch(
        self, texts: list[str], progress_callback: Any = None
    ) -> list[tuple[list[float], models.SparseVector | None]]:
        return [([0.1] * 128, None) for _ in texts]

    async def close(self) -> None:
        pass


class MockVectorStoreAdapter:
    def __init__(self):
        self.points = {}

    async def upsert_points(self, table: str, points: list[dict[str, Any]]) -> None:
        for p in points:
            self.points[p["id"]] = p

    async def search_vectors(
        self, table: str, vector: list[float], limit: int, where: str | None = None
    ) -> list[ScoredPoint]:
        # Simple mock return
        return [ScoredPoint(id=pid, score=0.9, payload=p["payload"]) for pid, p in self.points.items()]

    async def search_fulltext(self, table: str, query: str, limit: int, where: str | None = None) -> list[ScoredPoint]:
        return [ScoredPoint(id=pid, score=0.9, payload=p["payload"]) for pid, p in self.points.items()]

    async def delete_points(self, table: str, ids: list[int]) -> None:
        for pid in ids:
            self.points.pop(pid, None)

    async def delete_points_by_where(self, table: str, where_clause: str) -> None:
        # Simplified clean
        self.points.clear()

    async def filter_only(self, table: str, where_clause: str, limit: int) -> list[ScoredPoint]:
        return [ScoredPoint(id=pid, score=1.0, payload=p["payload"]) for pid, p in self.points.items()][:limit]


@pytest_asyncio.fixture
async def test_db():
    db = Database(Path(":memory:"))
    await db.connect()
    await db.init_schema()
    yield db
    await db.close()


@pytest.fixture
def test_settings():
    return Settings(
        app_access_token="test-token-12345678-long-enough-32-chars",
        session_secret_key="a" * 32,
        sqlite_db_path=Path(":memory:"),
        manticore_url="http://localhost:9308",
    )


@pytest.mark.asyncio
async def test_video_service_flow(test_db, test_settings):
    # Arrange
    video_repo = VideoRepository(test_db)
    chunk_repo = ChunkRepository(test_db)
    task_repo = TaskRepository(test_db)
    manticore = MockVectorStoreAdapter()
    embedder = MockEmbeddingAdapter()

    video_service = VideoService(
        db=test_db,
        video_repo=video_repo,
        chunk_repo=chunk_repo,
        task_repo=task_repo,
        manticore=manticore,
        embedder=embedder,
        settings=test_settings,
    )

    # 1. Upsert a video
    video_id = await video_repo.upsert(
        source_file_id="test_file_1",
        title="17.06.2026 Test Video",
        status="pending",
    )

    # 2. Add some chunks
    await chunk_repo.replace_chunks(
        video_id,
        [
            {"chunk_index": 0, "start_sec": 0.0, "end_sec": 10.0, "text": "Привет всем!"},
            {"chunk_index": 1, "start_sec": 10.0, "end_sec": 20.0, "text": "Сегодня мы пишем интеграционные тесты."},
        ],
    )

    # 3. Check details
    details = await video_service.get_video_details(video_id)
    assert details["title"] == "17.06.2026 Test Video"
    assert details["chunk_count"] == 2
    assert details["recorded_date"] == "2026-06-17"

    # 4. Mark video silent (should clean chunks and tasks)
    await video_service.mark_video_silent(video_id)

    details = await video_service.get_video_details(video_id)
    assert details["is_silent"] == 1
    assert details["chunk_count"] == 0


@pytest.mark.asyncio
async def test_search_service_flow(test_db, test_settings):
    # Arrange
    video_repo = VideoRepository(test_db)
    cache_repo = CacheRepository(test_db)
    manticore = MockVectorStoreAdapter()
    embedder = MockEmbeddingAdapter()

    search_service = SearchService(
        embedder=embedder,
        vector_store=manticore,
        video_repo=video_repo,
        cache_repo=cache_repo,
        settings=test_settings,
    )

    # Insert video and mock Manticore point
    video_id = await video_repo.upsert(
        source_file_id="test_file_search",
        title="Search Test Video",
        status="completed",
    )

    # Prepare mock points in manticore
    await manticore.upsert_points(
        "chunks",
        [
            {
                "id": 101,
                "payload": {
                    "video_id": video_id,
                    "chunk_id": 101,
                    "chunk_index": 0,
                    "start_sec": 0.0,
                    "end_sec": 5.0,
                    "text": "Поисковый тест",
                    "title": "Search Test Video",
                    "source_file_id": "test_file_search",
                },
            }
        ],
    )

    # Act
    results = await search_service.search("тест", limit=5)

    # Assert
    assert len(results) == 1
    assert results[0].title == "Search Test Video"
    assert results[0].chunk_id == 101
    assert "тест" in results[0].text
