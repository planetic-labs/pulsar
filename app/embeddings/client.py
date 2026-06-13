from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable

from app.config import EmbeddingSettings, get_sqlite_settings
from app.db import db_connection
from app.embeddings.factory import get_provider
from app.manticore import models
from app.repository import get_cached_embedding, save_cached_embedding

logger = logging.getLogger(__name__)

# Module-level L1 cache to persist across client re-instantiations
_L1_CACHE: OrderedDict[str, tuple[list[float], models.SparseVector | None]] = OrderedDict()


def clear_l1_cache() -> None:
    """Clears the global L1 cache. Primarily used for testing."""
    _L1_CACHE.clear()


class UnifiedEmbeddingClient:
    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        self.provider = get_provider(self.settings)

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

        # --- LEVEL 3: Remote API via Provider ---
        try:
            result = await self.provider.embed_text_async(text, task_type=task_type)
            dense, sparse = result

            # Save to L1 & L2
            self._update_l1(query_key, result)
            try:
                with db_connection(get_sqlite_settings()) as conn:
                    save_cached_embedding(conn, query_key, dense, sparse)
            except Exception as e:
                logger.warning(f"Cache L2 save failed: {e}")

            return result
        except Exception as e:
            logger.error(f"Embedding provider failed (async): {e}")
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

        # Remote via Provider
        try:
            result = self.provider.embed_text(text, task_type=task_type)
            dense, sparse = result

            self._update_l1(query_key, result)
            try:
                with db_connection(get_sqlite_settings()) as conn:
                    save_cached_embedding(conn, query_key, dense, sparse)
            except Exception as e:
                logger.warning(f"Cache L2 save failed (sync): {e}")

            return dense, sparse
        except Exception as e:
            logger.error(f"Embedding provider failed: {e}")
            raise e

    async def embed_batch_async(
        self,
        texts: list[str],
        task_type: str = "RETRIEVAL_DOCUMENT",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[tuple[list[float], models.SparseVector | None]]:

        if not texts:
            return []

        try:
            return await self.provider.embed_batch_async(
                texts, task_type=task_type, progress_callback=progress_callback
            )
        except Exception as e:
            logger.error(f"Embedding provider batch failed (async): {e}")
            raise e
