import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client import models

from app.search import hybrid_search


@pytest.mark.asyncio
async def test_hybrid_search_filter_4k(mocker):
    # Mock database connection
    mock_conn = MagicMock(spec=sqlite3.Connection)

    # Mock settings
    mock_q_settings = MagicMock()
    mock_q_settings.collection_name = "test_chunks"
    mocker.patch("app.search.get_qdrant_settings", return_value=mock_q_settings)

    mock_e_settings = MagicMock()
    mock_e_settings.dimension = 1024
    mocker.patch("app.search.get_embedding_settings", return_value=mock_e_settings)

    # Mock QdrantClient methods
    mock_qdrant_client = MagicMock()
    # Mock query_points to return dummy data
    mock_points_res = MagicMock()
    mock_points_res.points = [
        models.ScoredPoint(
            id=1,
            version=1,
            score=0.9,
            payload={
                "chunk_id": 1,
                "video_id": 10,
                "transcript_id": 100,
                "chunk_index": 0,
                "start_sec": 10.0,
                "end_sec": 15.0,
                "text": "test chunk",
                "title": "Test Video 4K",
                "is_short": False,
                "is_4k": True,
            },
        )
    ]
    mock_qdrant_client.query_points.return_value = mock_points_res
    mocker.patch("app.search.get_qdrant_client", return_value=mock_qdrant_client)

    # Mock UnifiedEmbeddingClient
    mock_embed_instance = MagicMock()
    mock_embed_instance.embed_text_async = AsyncMock(return_value=([0.1] * 1024, None))
    mocker.patch("app.search.UnifiedEmbeddingClient", return_value=mock_embed_instance)

    # Run hybrid search with video_type="4k"
    results = await hybrid_search(
        mock_conn,
        "test query",
        search_mode="semantic",
        video_type="4k",
    )

    # Verify that query_points was called with correct filter for is_4k
    mock_qdrant_client.query_points.assert_called_once()
    args, _ = mock_qdrant_client.query_points.call_args

    # Extract the query filter
    q_filter = args[4]

    assert q_filter is not None
    assert isinstance(q_filter, models.Filter)

    # Check that is_4k filter is present in must conditions
    is_4k_filter = None
    for condition in q_filter.must:
        if isinstance(condition, models.FieldCondition) and condition.key == "is_4k":
            is_4k_filter = condition
            break

    assert is_4k_filter is not None
    assert is_4k_filter.match.value is True

    # Verify results mapping
    assert len(results) == 1
    assert results[0].is_4k is True
    assert results[0].title == "Test Video 4K"
