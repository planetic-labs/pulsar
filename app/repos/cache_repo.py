from __future__ import annotations

import json

from app.database import Database
from app.manticore import models


class CacheRepository:
    """Репозиторий для кэширования векторов поисковых запросов в БД."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_embedding(self, query: str) -> tuple[list[float], models.SparseVector | None] | None:
        """Получает эмбеддинг из кэша, если он существует."""
        sql = "SELECT dense_vector, sparse_indices, sparse_values FROM query_cache WHERE query = ?"
        async with self.db.transaction() as conn:
            async with conn.execute(sql, (query,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None

                dense = json.loads(row["dense_vector"])
                sparse = None
                if row["sparse_indices"] and row["sparse_values"]:
                    sparse = models.SparseVector(
                        indices=json.loads(row["sparse_indices"]),
                        values=json.loads(row["sparse_values"]),
                    )
                return dense, sparse

    async def save_embedding(self, query: str, dense: list[float], sparse: models.SparseVector | None) -> None:
        """Сохраняет эмбеддинг в SQLite кэш."""
        s_indices = json.dumps(sparse.indices) if sparse else None
        s_values = json.dumps(sparse.values) if sparse else None
        sql = """
            INSERT OR REPLACE INTO query_cache (query, dense_vector, sparse_indices, sparse_values)
            VALUES (?, ?, ?, ?)
        """
        async with self.db.transaction() as conn:
            await conn.execute(sql, (query, json.dumps(dense), s_indices, s_values))
