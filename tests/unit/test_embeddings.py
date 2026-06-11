from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import EmbeddingSettings
from app.embeddings.custom import CustomEmbeddingProvider
from app.embeddings.factory import get_provider
from app.embeddings.openai import OpenAIEmbeddingProvider


def test_get_provider():
    settings_custom = EmbeddingSettings(api_url="http://test", api_token="tok", provider="custom")
    provider = get_provider(settings_custom)
    assert isinstance(provider, CustomEmbeddingProvider)

    settings_openai = EmbeddingSettings(api_url="http://test", api_token="tok", provider="openai")
    provider = get_provider(settings_openai)
    assert isinstance(provider, OpenAIEmbeddingProvider)

    settings_unknown = EmbeddingSettings(api_url="http://test", api_token="tok", provider="unknown")
    with pytest.raises(ValueError, match="Unknown embedding provider: unknown"):
        get_provider(settings_unknown)


def test_custom_provider_embed_text_sync(mocker):
    settings = EmbeddingSettings(api_url="http://test-api", api_token="tok", provider="custom")
    provider = CustomEmbeddingProvider(settings)

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [{"embedding": [0.1, 0.2], "embeddings_sparse": {"indices": [1, 2], "values": [0.5, 0.6]}}],
        "usage": {},
    }
    mock_response.status_code = 200

    mock_post = mocker.patch("httpx.Client.post", return_value=mock_response)

    dense, sparse = provider.embed_text("hello")
    assert dense == [0.1, 0.2]
    assert sparse is not None
    assert sparse.indices == [1, 2]
    assert sparse.values == [0.5, 0.6]
    assert mock_post.call_args[0][0] == "http://test-api/embeddings"


@pytest.mark.asyncio
async def test_custom_provider_embed_text_async(mocker):
    settings = EmbeddingSettings(api_url="http://test-api/embeddings/", api_token="tok", provider="custom")
    provider = CustomEmbeddingProvider(settings)

    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": [0.3, 0.4]}], "usage": {}}
    mock_response.status_code = 200

    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response)

    dense, sparse = await provider.embed_text_async("world")
    assert dense == [0.3, 0.4]
    assert sparse is None
    # Check that it doesn't double-append /embeddings
    assert mock_post.call_args[0][0] == "http://test-api/embeddings"


@pytest.mark.asyncio
async def test_custom_provider_embed_batch_async(mocker):
    settings = EmbeddingSettings(api_url="http://test-api", api_token="tok", provider="custom")
    provider = CustomEmbeddingProvider(settings)

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [{"embedding": [0.1], "embeddings_sparse": {"indices": [1], "values": [0.9]}}, {"embedding": [0.2]}]
    }
    mock_response.status_code = 200

    mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response)

    results = await provider.embed_batch_async(["a", "b"])
    assert len(results) == 2
    assert results[0][0] == [0.1]
    assert results[0][1] is not None
    assert results[0][1].indices == [1]
    assert results[1][0] == [0.2]
    assert results[1][1] is None


def test_openai_provider_embed_text_sync(mocker):
    settings = EmbeddingSettings(api_url="http://test-api", api_token="tok", provider="openai")
    provider = OpenAIEmbeddingProvider(settings)

    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": [0.5, 0.6]}], "usage": {}}
    mock_response.status_code = 200

    mock_post = mocker.patch("httpx.Client.post", return_value=mock_response)

    dense, sparse = provider.embed_text("hello")
    assert dense == [0.5, 0.6]
    assert sparse is None
    assert mock_post.call_args[0][0] == "http://test-api/embeddings"


@pytest.mark.asyncio
async def test_openai_provider_embed_text_async(mocker):
    settings = EmbeddingSettings(api_url="http://test-api", api_token="tok", provider="openai")
    provider = OpenAIEmbeddingProvider(settings)

    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": [0.7, 0.8]}], "usage": {}}
    mock_response.status_code = 200

    mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response)

    dense, sparse = await provider.embed_text_async("world")
    assert dense == [0.7, 0.8]
    assert sparse is None


@pytest.mark.asyncio
async def test_openai_provider_embed_batch_async(mocker):
    settings = EmbeddingSettings(api_url="http://test-api", api_token="tok", provider="openai")
    provider = OpenAIEmbeddingProvider(settings)

    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": [0.11]}, {"embedding": [0.22]}]}
    mock_response.status_code = 200

    mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response)

    results = await provider.embed_batch_async(["a", "b"])
    assert len(results) == 2
    assert results[0][0] == [0.11]
    assert results[0][1] is None
    assert results[1][0] == [0.22]
    assert results[1][1] is None
