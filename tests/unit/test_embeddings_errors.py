from __future__ import annotations

import httpx
import pytest

from app.embeddings.base import EmbeddingProviderError, handle_api_errors


def test_handle_api_errors_401() -> None:
    # 401 Unauthorized
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/embeddings")
    response = httpx.Response(401, request=request)

    with pytest.raises(EmbeddingProviderError) as exc_info, handle_api_errors():
        raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

    assert "Ошибка авторизации (401 Unauthorized) при обращении к OpenRouter" in str(exc_info.value)
    assert "EMBEDDING_API_TOKEN" in str(exc_info.value)


def test_handle_api_errors_429() -> None:
    # 429 Too Many Requests
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/embeddings")
    response = httpx.Response(429, request=request)

    with pytest.raises(EmbeddingProviderError) as exc_info, handle_api_errors():
        raise httpx.HTTPStatusError("Too Many Requests", request=request, response=response)

    assert "Превышен лимит запросов или закончились средства на балансе (429 Too Many Requests)" in str(exc_info.value)
    assert "OpenRouter" in str(exc_info.value)


def test_handle_api_errors_500() -> None:
    # Другой статус код, например 500 Internal Server Error
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/embeddings")
    response = httpx.Response(500, request=request, text="Internal Server Error Detail")

    with pytest.raises(EmbeddingProviderError) as exc_info, handle_api_errors():
        raise httpx.HTTPStatusError("Internal Server Error", request=request, response=response)

    assert "Ошибка OpenRouter (500): Internal Server Error Detail" in str(exc_info.value)


def test_handle_api_errors_request_error() -> None:
    # Сетевая ошибка RequestError
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/embeddings")

    with pytest.raises(EmbeddingProviderError) as exc_info, handle_api_errors():
        raise httpx.RequestError("Connection refused", request=request)

    assert "Сетевая ошибка при обращении к OpenRouter" in str(exc_info.value)
    assert "Connection refused" in str(exc_info.value)


def test_handle_api_errors_other_domain() -> None:
    # Ошибка к другому домену
    request = httpx.Request("POST", "https://other-provider.com/embeddings")
    response = httpx.Response(401, request=request)

    with pytest.raises(EmbeddingProviderError) as exc_info, handle_api_errors():
        raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

    assert "Ошибка авторизации (401 Unauthorized) при обращении к API эмбеддингов" in str(exc_info.value)
