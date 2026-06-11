from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from app.config import EmbeddingSettings
from app.embeddings.base import BaseEmbeddingProvider
from app.manticore import models

logger = logging.getLogger(__name__)


class CustomEmbeddingProvider(BaseEmbeddingProvider):
    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        if not self.settings.api_url:
            raise ValueError("EMBEDDING_API_URL must be set for custom remote embeddings.")

    def _get_url(self) -> str:
        url = self.settings.api_url.rstrip("/")
        if not url.endswith("/embeddings"):
            url = f"{url}/embeddings"
        return url

    async def embed_text_async(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        logger.info(f"Custom AI: Embedding query from remote ({text[:20]}...)")
        url = self._get_url()
        headers = {"Authorization": f"Bearer {self.settings.api_token}"} if self.settings.api_token else {}
        payload = {"model": self.settings.model_id, "input": [text]}

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            res = response.json()

        dense = res["data"][0]["embedding"]
        sparse = None
        if "usage" in res and "embeddings_sparse" in res["data"][0]:
            sparse_data = res["data"][0]["embeddings_sparse"]
            sparse = models.SparseVector(indices=sparse_data["indices"], values=sparse_data["values"])

        return dense, sparse

    def embed_text(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        logger.info(f"Custom AI: Embedding query from remote (sync) ({text[:20]}...)")
        url = self._get_url()
        headers = {"Authorization": f"Bearer {self.settings.api_token}"} if self.settings.api_token else {}
        payload = {"model": self.settings.model_id, "input": [text]}

        with httpx.Client(timeout=60.0) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            res = response.json()

        dense = res["data"][0]["embedding"]
        sparse = None
        if "usage" in res and "embeddings_sparse" in res["data"][0]:
            sparse_data = res["data"][0]["embeddings_sparse"]
            sparse = models.SparseVector(indices=sparse_data["indices"], values=sparse_data["values"])

        return dense, sparse

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
                    f"Custom AI: Обработка батча {i // batch_size + 1} (фрагменты {i} - {current_end} из {total})..."
                )

                if progress_callback:
                    progress_callback(i, total)

                payload = {"model": self.settings.model_id, "input": batch}
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                res = response.json()

                for item in res["data"]:
                    dense = item["embedding"]
                    sparse = None
                    if "embeddings_sparse" in item:
                        s = item["embeddings_sparse"]
                        sparse = models.SparseVector(indices=s["indices"], values=s["values"])
                    results.append((dense, sparse))

        if progress_callback:
            progress_callback(total, total)

        return results
