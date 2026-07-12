from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator

import httpx

from app.config import EmbeddingSettings
from app.manticore import models


class EmbeddingProviderError(Exception):
    """Исключение, выбрасываемое при ошибках провайдеров эмбеддингов (например, OpenRouter)."""

    pass


@contextlib.contextmanager
def handle_api_errors() -> Generator[None, None, None]:
    try:
        yield
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        url = str(e.request.url)

        # Получаем понятное имя хоста/сервиса
        service_name = "OpenRouter" if "openrouter.ai" in url else "API эмбеддингов"

        if status_code == 401:
            msg = (
                f"Ошибка авторизации (401 Unauthorized) при обращении к {service_name}. "
                "Пожалуйста, убедитесь, что в конфигурации (.env) указан корректный токен доступа "
                "в переменной EMBEDDING_API_TOKEN."
            )
        elif status_code == 429:
            msg = (
                f"Превышен лимит запросов или закончились средства на балансе (429 Too Many Requests) "
                f"при обращении к {service_name}."
            )
        elif status_code == 403:
            msg = (
                f"Доступ к {service_name} запрещен (403 Forbidden). "
                "Проверьте права вашего API-ключа и настройки доступа в личном кабинете."
            )
        elif status_code == 400:
            msg = f"Неверный запрос (400 Bad Request) к {service_name}. Детали ошибки от сервера: {e.response.text}"
        else:
            msg = f"Ошибка {service_name} ({status_code}): {e.response.text}"
        raise EmbeddingProviderError(msg) from e
    except httpx.RequestError as e:
        url = str(e.request.url) if e.request else ""
        service_name = "OpenRouter" if "openrouter.ai" in url else "API эмбеддингов"
        raise EmbeddingProviderError(f"Сетевая ошибка при обращении к {service_name}: {e}") from e


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
