from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import httpx
from qdrant_client import models

from app.config import EmbeddingSettings, get_sqlite_settings
from app.db import db_connection
from app.repository import get_cached_embedding, save_cached_embedding

logger = logging.getLogger(__name__)

# Module-level L1 cache to persist across client re-instantiations
_L1_CACHE: OrderedDict[str, tuple[list[float], models.SparseVector | None]] = OrderedDict()


class UnifiedEmbeddingClient:
    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        if not self.settings.api_url:
            raise ValueError("EMBEDDING_API_URL must be set for remote embeddings.")

    async def embed_text_async(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        """Returns (dense_vector, sparse_vector) with tiered caching (L1: Memory, L2: SQLite)."""
        query_key = f"{task_type}:{text}"

        # --- LEVEL 1: Memory (LRU) ---
        if query_key in _L1_CACHE:
            # Move to end (most recent)
            val = _L1_CACHE.pop(query_key)
            _L1_CACHE[query_key] = val
            return val

        # --- LEVEL 2: SQLite ---
        try:
            with db_connection(get_sqlite_settings()) as conn:
                cached = get_cached_embedding(conn, query_key)
                if cached:
                    # Save to L1 and return
                    self._update_l1(query_key, cached)
                    return cached
        except Exception as e:
            logger.warning(f"Cache L2 lookup failed: {e}")

        # --- LEVEL 3: Remote API ---
        logger.info(f"AI: Embedding query from remote ({text[:20]}...)")
        url = f"{self.settings.api_url.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {self.settings.api_token}"} if self.settings.api_token else {}
        payload = {"model": self.settings.model_id, "input": [text]}

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                res = response.json()

            dense = res["data"][0]["embedding"]
            sparse = None
            if "usage" in res and "embeddings_sparse" in res["data"][0]:
                sparse_data = res["data"][0]["embeddings_sparse"]
                sparse = models.SparseVector(indices=sparse_data["indices"], values=sparse_data["values"])

            result = (dense, sparse)

            # Save to L1 & L2
            self._update_l1(query_key, result)
            try:
                with db_connection(get_sqlite_settings()) as conn:
                    save_cached_embedding(conn, query_key, dense, sparse)
            except Exception as e:
                logger.warning(f"Cache L2 save failed: {e}")

            return result
        except Exception as e:
            logger.error(f"Remote embedding failed (async): {e}")
            raise e

    def _update_l1(self, key: str, value: tuple[list[float], models.SparseVector | None]) -> None:
        """Updates module-level LRU cache."""
        if key in _L1_CACHE:
            _L1_CACHE.pop(key)
        _L1_CACHE[key] = value
        # Enforce size limit from settings
        while len(_L1_CACHE) > self.settings.cache_lru_size:
            _L1_CACHE.popitem(last=False)

    def embed_text(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        """Returns (dense_vector, sparse_vector) for a single text with caching."""
        query_key = f"{task_type}:{text}"

        # L1 Check
        if query_key in _L1_CACHE:
            val = _L1_CACHE.pop(query_key)
            _L1_CACHE[query_key] = val
            return val

        # L2 Check
        try:
            with db_connection(get_sqlite_settings()) as conn:
                cached = get_cached_embedding(conn, query_key)
                if cached:
                    self._update_l1(query_key, cached)
                    return cached
        except Exception as e:
            logger.warning(f"Cache L2 lookup failed (sync): {e}")

        # Remote
        logger.info(f"AI: Embedding query from remote (sync) ({text[:20]}...)")
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
            if "usage" in res and "embeddings_sparse" in res["data"][0]:
                sparse_data = res["data"][0]["embeddings_sparse"]
                sparse = models.SparseVector(indices=sparse_data["indices"], values=sparse_data["values"])

            result = (dense, sparse)
            self._update_l1(query_key, result)
            try:
                with db_connection(get_sqlite_settings()) as conn:
                    save_cached_embedding(conn, query_key, dense, sparse)
            except Exception as e:
                logger.warning(f"Cache L2 save failed (sync): {e}")

            return dense, sparse
        except Exception as e:
            logger.error(f"Remote embedding failed: {e}")
            raise e

    async def embed_batch_async(
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
        batch_size = 50  # Restored to 50 as requested
        async with httpx.AsyncClient(timeout=300.0) as client:
            for i in range(0, total, batch_size):
                batch = texts[i : i + batch_size]
                current_end = min(i + batch_size, total)
                logger.info(f"AI: Обработка батча {i // batch_size + 1} (фрагменты {i} - {current_end} из {total})...")

                if progress_callback:
                    progress_callback(i, total)

                payload = {"model": self.settings.model_id, "input": batch}
                try:
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
                except Exception as e:
                    logger.error(f"Remote batch embedding failed (async): {e}")
                    raise e

        if progress_callback:
            progress_callback(total, total)

        return results


# For backward compatibility during migration
GeminiEmbeddingClient = UnifiedEmbeddingClient
