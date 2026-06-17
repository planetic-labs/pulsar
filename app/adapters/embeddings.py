from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.config import EmbeddingSettings
from app.embeddings.client import UnifiedEmbeddingClient
from app.ports import EmbeddingPort

logger = logging.getLogger("app.adapters.embeddings")


class EmbeddingAdapter(EmbeddingPort):
    """Адаптер для службы генерации эмбеддингов, реализующий EmbeddingPort."""

    def __init__(self, settings: EmbeddingSettings) -> None:
        self._client = UnifiedEmbeddingClient(settings)

    async def embed_text(self, text: str, task_type: str = "RETRIEVAL_QUERY") -> tuple[list[float], Any]:
        """Генерирует плотный и разреженный вектора для одного текста."""
        return await self._client.embed_text_async(text, task_type=task_type)

    async def embed_batch(
        self, texts: list[str], progress_callback: Callable[[int, int], None] | None = None
    ) -> list[tuple[list[float], Any]]:
        """Генерирует плотные и разреженные вектора для пакета текстов."""
        return await self._client.embed_batch_async(
            texts, task_type="RETRIEVAL_DOCUMENT", progress_callback=progress_callback
        )
