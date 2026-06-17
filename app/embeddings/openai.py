from __future__ import annotations

import asyncio
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
        formatted_text = self._format_text(text, task_type)
        payload = self._build_payload([formatted_text])

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
        formatted_text = self._format_text(text, task_type)
        payload = self._build_payload([formatted_text])

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

        total = len(texts)
        batch_size = 500  # Увеличиваем батч до 500
        results: list[tuple[list[float], models.SparseVector | None] | None] = [None] * total
        completed_count = 0

        async def process_batch(start_idx: int, batch_texts: list[str], client: httpx.AsyncClient) -> None:
            nonlocal completed_count
            current_end = min(start_idx + len(batch_texts), total)
            logger.info(f"OpenAI: Параллельный запрос батча (фрагменты {start_idx} - {current_end} из {total})....")

            formatted_batch = self._format_texts(batch_texts, task_type)
            payload = self._build_payload(formatted_batch)
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            res = response.json()

            sorted_data = sorted(res["data"], key=lambda x: x.get("index", 0))
            for idx, item in enumerate(sorted_data):
                dense = item["embedding"]
                global_idx = start_idx + idx
                if global_idx < total:
                    results[global_idx] = (dense, None)

            completed_count += len(batch_texts)
            if progress_callback:
                progress_callback(completed_count, total)

        async with httpx.AsyncClient(timeout=300.0) as client:
            tasks = []
            for i in range(0, total, batch_size):
                batch = texts[i : i + batch_size]
                tasks.append(process_batch(i, batch, client))

            await asyncio.gather(*tasks)

        # Гарантируем, что все элементы заполнены
        final_results = [r for r in results if r is not None]
        return final_results
