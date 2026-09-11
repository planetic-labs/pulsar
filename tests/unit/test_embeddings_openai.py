from __future__ import annotations

from app.config import EmbeddingSettings
from app.embeddings.openai import OpenAIEmbeddingProvider


def test_proxy_credentials_are_sent_in_separate_headers() -> None:
    provider = OpenAIEmbeddingProvider(
        EmbeddingSettings(
            api_url="https://orp.pre.m6z.ru/api/v1",
            api_token="",
            openrouter_api_key="openrouter-key",
            proxy_token="proxy-token",
        )
    )

    headers = provider._get_headers()

    assert headers["Authorization"] == "Bearer openrouter-key"
    assert headers["X-Proxy-Token"] == "proxy-token"


def test_proxy_header_is_omitted_when_not_configured() -> None:
    provider = OpenAIEmbeddingProvider(
        EmbeddingSettings(
            api_url="http://infinity:7997/v1",
            api_token="embedding-token",
        )
    )

    assert "X-Proxy-Token" not in provider._get_headers()


def test_openrouter_providers_are_ordered_with_fallbacks() -> None:
    provider = OpenAIEmbeddingProvider(
        EmbeddingSettings(
            api_url="https://openrouter-proxy.example.workers.dev/v1",
            api_token="proxy-token",
            openrouter_providers=["nebius", "deepinfra"],
        )
    )

    payload = provider._build_payload(["query"])

    assert payload["provider"] == {
        "order": ["nebius", "deepinfra"],
        "only": ["nebius", "deepinfra"],
        "allow_fallbacks": True,
    }
