from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
from qdrant_client import models

from app.config import EmbeddingSettings

logger = logging.getLogger(__name__)


class UnifiedEmbeddingClient:
    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        if not self.settings.api_url:
            raise ValueError("EMBEDDING_API_URL must be set for remote embeddings.")
        logger.info(f"Using Remote Embedding Service at {self.settings.api_url}")

    def embed_text(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        """Returns (dense_vector, sparse_vector) for a single text."""
        url = f"{self.settings.api_url.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {self.settings.api_token}"} if self.settings.api_token else {}
        payload = {"model": self.settings.model_id, "input": [text]}

        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                res = response.json()

            dense = res["data"][0]["embedding"]
            sparse = None
            # Infinity specific sparse handling
            if "usage" in res and "embeddings_sparse" in res["data"][0]:
                sparse_data = res["data"][0]["embeddings_sparse"]
                sparse = models.SparseVector(indices=sparse_data["indices"], values=sparse_data["values"])
            return dense, sparse
        except Exception as e:
            logger.error(f"Remote embedding failed: {e}")
            raise e

    def embed_batch(
        self,
        texts: list[str],
        task_type: str = "RETRIEVAL_DOCUMENT",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[tuple[list[float], models.SparseVector | None]]:

        if not texts:
            return []

        url = f"{self.settings.api_url.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {self.settings.api_token}"} if self.settings.api_token else {}

        results = []
        total = len(texts)
        for i in range(0, total, 50):
            batch = texts[i : i + 50]
            current_end = min(i + 50, total)
            logger.info(f"AI: Обработка батча {i // 50 + 1} (фрагменты {i} - {current_end} из {total})...")

            if progress_callback:
                progress_callback(i, total)

            payload = {"model": self.settings.model_id, "input": batch}
            try:
                with httpx.Client(timeout=120.0) as client:
                    response = client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    res = response.json()

                for item in res["data"]:
                    dense = item["embedding"]
                    sparse = None
                    if "embeddings_sparse" in item:
                        s = item["embeddings_sparse"]
                        sparse = models.SparseVector(indices=s["indices"], values=s["values"])
                    results.append((dense, sparse))
            except Exception as e:
                logger.error(f"Remote batch embedding failed: {e}")
                raise e

        if progress_callback:
            progress_callback(total, total)

        return results


# For backward compatibility during migration
GeminiEmbeddingClient = UnifiedEmbeddingClient
