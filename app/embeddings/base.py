from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from app.config import EmbeddingSettings
from app.manticore import models


class BaseEmbeddingProvider(ABC):
    @abstractmethod
    def __init__(self, settings: EmbeddingSettings) -> None:
        pass

    @abstractmethod
    async def embed_text_async(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        """Asynchronously embed a single text, returning (dense_vector, sparse_vector)."""
        pass

    @abstractmethod
    def embed_text(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        """Synchronously embed a single text, returning (dense_vector, sparse_vector)."""
        pass

    @abstractmethod
    async def embed_batch_async(
        self,
        texts: list[str],
        task_type: str = "RETRIEVAL_DOCUMENT",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[tuple[list[float], models.SparseVector | None]]:
        """Asynchronously embed a batch of texts, returning list of (dense_vector, sparse_vector)."""
        pass
