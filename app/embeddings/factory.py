from __future__ import annotations

from app.config import EmbeddingSettings
from app.embeddings.base import BaseEmbeddingProvider
from app.embeddings.custom import CustomEmbeddingProvider
from app.embeddings.openai import OpenAIEmbeddingProvider


def get_provider(settings: EmbeddingSettings) -> BaseEmbeddingProvider:
    provider_type = settings.provider.lower()
    if provider_type == "openai":
        return OpenAIEmbeddingProvider(settings)
    elif provider_type == "custom":
        return CustomEmbeddingProvider(settings)
    else:
        raise ValueError(f"Unknown embedding provider: {settings.provider}")
