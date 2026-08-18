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
        async with self.db.transaction() as conn, conn.execute(sql, (video_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_by_video_id(self, video_id: int) -> None:
        """Удаляет все чанки для указанного видео."""
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))

    async def update_chunk(self, chunk_id: int, text: str) -> None:
        """Обновляет текст конкретного чанка в SQLite."""
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE chunks SET text = ? WHERE id = ?", (text, chunk_id))

    async def get_chunk_by_id(self, chunk_id: int) -> dict[str, Any] | None:
        """Возвращает чанк по его ID."""
        sql = "SELECT * FROM chunks WHERE id = ?"
        async with self.db.transaction() as conn, conn.execute(sql, (chunk_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_flag(self, chunk_id: int) -> None:
        """Создает жалобу на чанк (уникальный chunk_id)."""
        sql = "INSERT OR IGNORE INTO subtitle_flags (chunk_id) VALUES (?)"
        async with self.db.transaction() as conn:
            await conn.execute(sql, (chunk_id,))

    async def delete_flag(self, chunk_id: int) -> None:
        """Удаляет жалобу на чанк."""
        sql = "DELETE FROM subtitle_flags WHERE chunk_id = ?"
        async with self.db.transaction() as conn:
            await conn.execute(sql, (chunk_id,))

    async def get_all_flags(self) -> list[dict[str, Any]]:
        """Возвращает все активные жалобы с метаданными чанков и видео."""
        sql = """
            SELECT
                f.id as flag_id,
                f.chunk_id,
                f.created_at,
                c.video_id,
                c.chunk_index,
                c.start_sec,
                c.end_sec,
                c.text as original_text,
                v.title as video_title
            FROM subtitle_flags f
            JOIN chunks c ON f.chunk_id = c.id
            JOIN videos v ON c.video_id = v.id
            ORDER BY f.created_at DESC
        """
        async with self.db.transaction() as conn, conn.execute(sql) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def reserve_and_get_next_flag(self, user_id: str, lock_timeout_sec: int) -> dict[str, Any] | None:
        """Находит, резервирует и возвращает следующую свободную жалобу для конкретного пользователя."""
        find_sql = """
            SELECT f.chunk_id
            FROM subtitle_flags f
            WHERE f.locked_by = ?
               OR f.locked_by IS NULL
               OR datetime(f.locked_at, ? || ' seconds') < datetime('now')
            ORDER BY
                CASE WHEN f.locked_by = ? THEN 0 ELSE 1 END,
                f.created_at ASC
            LIMIT 1
        """
        async with self.db.transaction() as conn:
            async with conn.execute(find_sql, (user_id, lock_timeout_sec, user_id)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                chunk_id = row["chunk_id"]

            update_sql = """
                UPDATE subtitle_flags
                SET locked_by = ?, locked_at = datetime('now')
                WHERE chunk_id = ?
            """
            await conn.execute(update_sql, (user_id, chunk_id))

            meta_sql = """
                SELECT
                    f.id as flag_id,
                    f.chunk_id,
                    f.created_at,
                    f.locked_by,
                    f.locked_at,
                    c.video_id,
                    c.chunk_index,
                    c.start_sec,
                    c.end_sec,
                    c.text as original_text,
                    v.title as video_title
                FROM subtitle_flags f
                JOIN chunks c ON f.chunk_id = c.id
                JOIN videos v ON c.video_id = v.id
                WHERE f.chunk_id = ?
            """
            async with conn.execute(meta_sql, (chunk_id,)) as cursor:
                meta_row = await cursor.fetchone()
                return dict(meta_row) if meta_row else None
