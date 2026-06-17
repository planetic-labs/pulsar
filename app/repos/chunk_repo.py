from __future__ import annotations

from typing import Any

from app.database import Database


class ChunkRepository:
    """Репозиторий для работы с чанками (сегментами) видео."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def replace_chunks(self, video_id: int, chunks: list[dict[str, Any]]) -> None:
        """Атомарно удаляет старые чанки для видео и вставляет новые."""
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))
            await conn.executemany(
                """
                INSERT INTO chunks (
                    video_id, chunk_index,
                    start_sec, end_sec, text
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        video_id,
                        int(c["chunk_index"]),
                        float(c["start_sec"]),
                        float(c["end_sec"]),
                        c["text"],
                    )
                    for c in chunks
                ],
            )

    async def get_by_video_id(self, video_id: int) -> list[dict[str, Any]]:
        """Возвращает все чанки для видео, упорядоченные по индексу."""
        sql = "SELECT * FROM chunks WHERE video_id = ? ORDER BY chunk_index ASC"
        async with self.db.transaction() as conn:
            async with conn.execute(sql, (video_id,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def delete_by_video_id(self, video_id: int) -> None:
        """Удаляет все чанки для указанного видео."""
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))
