from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import EmbeddingSettings
from app.gemini import UnifiedEmbeddingClient


@pytest.fixture
def embed_settings():
    return EmbeddingSettings(api_url="http://test-api", api_token="test-token", model_id="test-model")


def test_embed_text_sync(embed_settings, mocker):
    client = UnifiedEmbeddingClient(embed_settings)

    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": [0.1, 0.2]}], "usage": {}}
    mock_response.status_code = 200

    # Mock httpx.Client.post
    mock_client = mocker.patch("httpx.Client.post", return_value=mock_response)

    dense, sparse = client.embed_text("hello")
    assert dense == [0.1, 0.2]
    assert sparse is None
    assert mock_client.called


@pytest.mark.asyncio
async def test_embed_text_async(embed_settings, mocker):
    client = UnifiedEmbeddingClient(embed_settings)

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [{"embedding": [0.3, 0.4], "embeddings_sparse": {"indices": [1], "values": [0.5]}}],
        "usage": {},
    }
    mock_response.status_code = 200

    # Mock httpx.AsyncClient.post
    mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response)

    dense, sparse = await client.embed_text_async("world")
    assert dense == [0.3, 0.4]
    assert sparse is not None
    assert sparse.indices == [1]
    assert sparse.values == [0.5]


@pytest.mark.asyncio
async def test_embed_batch_async(embed_settings, mocker):
    client = UnifiedEmbeddingClient(embed_settings)

    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": [0.1]}, {"embedding": [0.2]}]}
    mock_response.status_code = 200

    mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response)

    texts = ["a", "b"]
    results = await client.embed_batch_async(texts)
    assert len(results) == 2
    assert results[0][0] == [0.1]
    assert results[1][0] == [0.2]
