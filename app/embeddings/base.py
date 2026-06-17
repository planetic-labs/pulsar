from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from app.config import EmbeddingSettings
from app.manticore import models


class BaseEmbeddingProvider(ABC):
    @abstractmethod
    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings

    def _format_text(self, text: str, task_type: str) -> str:
        model_lower = (self.settings.model_id or "").lower()
        if "qwen" in model_lower:
            if task_type == "RETRIEVAL_QUERY":
                instruction = "Instruct: Given a web search query, retrieve relevant passages that answer the query"
                return f"{instruction}\nQuery: {text}"
            return text
        elif "e5" in model_lower:
            if task_type == "RETRIEVAL_QUERY":
                return f"query: {text}"
            return f"passage: {text}"
        elif "bge" in model_lower:
            if task_type == "RETRIEVAL_QUERY":
                return f"Represent this sentence for searching relevant passages: {text}"
            return text
        return text

    def _format_texts(self, texts: list[str], task_type: str) -> list[str]:
        return [self._format_text(t, task_type) for t in texts]

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
