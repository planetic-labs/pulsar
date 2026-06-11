from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx

from app.config import EmbeddingSettings
from app.embeddings.base import BaseEmbeddingProvider
from app.manticore import models

logger = logging.getLogger(__name__)


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):
    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        if not self.settings.api_url:
            raise ValueError("EMBEDDING_API_URL must be set for OpenAI embeddings.")

    def _get_url(self) -> str:
        url = self.settings.api_url.rstrip("/")
        if not url.endswith("/embeddings"):
            url = f"{url}/embeddings"
        return url

    def _build_payload(self, input_data: list[str]) -> dict:
        payload: dict[str, Any] = {"model": self.settings.model_id, "input": input_data}
        if self.settings.openrouter_providers:
            payload["provider"] = {"only": self.settings.openrouter_providers, "allow_fallbacks": False}
        return payload

    async def embed_text_async(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        logger.info(f"OpenAI: Embedding query from remote ({text[:20]}...)")
        url = self._get_url()
        headers = {"Authorization": f"Bearer {self.settings.api_token}"} if self.settings.api_token else {}
        payload = self._build_payload([text])

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            res = response.json()

        dense = res["data"][0]["embedding"]
        return dense, None

    def embed_text(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        logger.info(f"OpenAI: Embedding query from remote (sync) ({text[:20]}...)")
        url = self._get_url()
        headers = {"Authorization": f"Bearer {self.settings.api_token}"} if self.settings.api_token else {}
        payload = self._build_payload([text])

        with httpx.Client(timeout=60.0) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            res = response.json()

        dense = res["data"][0]["embedding"]
        return dense, None

    async def embed_batch_async(
        self,
        texts: list[str],
        task_type: str = "RETRIEVAL_DOCUMENT",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[tuple[list[float], models.SparseVector | None]]:
        if not texts:
            return []

        url = self._get_url()
        headers = {"Authorization": f"Bearer {self.settings.api_token}"} if self.settings.api_token else {}

        results: list[tuple[list[float], models.SparseVector | None]] = []
        total = len(texts)
        batch_size = 50
        async with httpx.AsyncClient(timeout=300.0) as client:
            for i in range(0, total, batch_size):
                batch = texts[i : i + batch_size]
                current_end = min(i + batch_size, total)
                logger.info(
                    f"OpenAI: Обработка батча {i // batch_size + 1} (фрагменты {i} - {current_end} из {total})..."
                )

                if progress_callback:
                    progress_callback(i, total)

                payload = self._build_payload(batch)
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                res = response.json()

                for item in res["data"]:
                    dense = item["embedding"]
                    results.append((dense, None))

        if progress_callback:
            progress_callback(total, total)

        return results
