from __future__ import annotations

from typing import Any

from app.chunking import CHUNKING_ALGORITHM_VERSION
from app.database import Database
from app.indexing_state import (
    chunk_content_hash,
    ensure_active_generation_async,
    stable_chunk_logical_id,
)


class ChunkRepository:
    """Репозиторий для работы с чанками (сегментами) видео."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def replace_chunks(self, video_id: int, chunks: list[dict[str, Any]]) -> None:
        """Атомарно обновляет чанки без смены ID у сохранившихся позиций."""
        async with self.db.transaction() as conn:
            async with conn.execute("SELECT source_file_id FROM videos WHERE id = ?", (video_id,)) as cursor:
                video = await cursor.fetchone()
            if not video:
                raise ValueError(f"Video {video_id} does not exist")

            generation_id = await ensure_active_generation_async(conn)
            async with conn.execute(
                "SELECT id, chunk_index, content_hash FROM chunks WHERE video_id = ?", (video_id,)
            ) as cursor:
                existing_rows = await cursor.fetchall()
            existing = {
                int(row["chunk_index"]): {"content_hash": row["content_hash"], "id": int(row["id"])}
                for row in existing_rows
            }
            retained_indices: set[int] = set()

            for chunk in chunks:
                chunk_index = int(chunk["chunk_index"])
                retained_indices.add(chunk_index)
                start_sec = float(chunk["start_sec"])
                end_sec = float(chunk["end_sec"])
                text = str(chunk["text"])
                logical_id = stable_chunk_logical_id(str(video["source_file_id"]), chunk_index)
                content_hash = chunk_content_hash(text=text, start_sec=start_sec, end_sec=end_sec)
                async with conn.execute(
                    """
                    INSERT INTO chunks (
                        video_id, chunk_index, start_sec, end_sec, text,
                        logical_id, content_hash, chunking_version, generation_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(video_id, chunk_index) DO UPDATE SET
                        start_sec = excluded.start_sec,
                        end_sec = excluded.end_sec,
                        text = excluded.text,
                        logical_id = excluded.logical_id,
                        content_hash = excluded.content_hash,
                        chunking_version = excluded.chunking_version,
                        generation_id = excluded.generation_id
                    RETURNING id
                    """,
                    (
                        video_id,
                        chunk_index,
                        start_sec,
                        end_sec,
                        text,
                        logical_id,
                        content_hash,
                        CHUNKING_ALGORITHM_VERSION,
                        generation_id,
                    ),
                ) as cursor:
                    row = await cursor.fetchone()
                assert row is not None
                event_key = f"upsert:{generation_id}:{row['id']}:{content_hash}"
                await conn.execute(
                    """
                    INSERT INTO index_outbox (
                        event_key, event_type, video_id, chunk_id, generation_id, payload
                    ) VALUES (?, 'upsert', ?, ?, ?, ?)
                    ON CONFLICT(event_key) DO NOTHING
                    """,
                    (
                        event_key,
                        video_id,
                        int(row["id"]),
                        generation_id,
                        f'{{"chunk_id":{int(row["id"])},"video_id":{video_id}}}',
                    ),
                )

            for chunk_index, old in existing.items():
                if chunk_index in retained_indices:
                    continue
                old_id = int(old["id"])
                event_key = f"delete:{generation_id}:{old_id}:{old['content_hash'] or 'legacy'}"
                await conn.execute(
                    """
                    INSERT INTO index_outbox (
                        event_key, event_type, video_id, chunk_id, generation_id, payload
                    ) VALUES (?, 'delete', ?, ?, ?, ?)
                    ON CONFLICT(event_key) DO NOTHING
                    """,
                    (event_key, video_id, old_id, generation_id, f'{{"chunk_id":{old_id},"video_id":{video_id}}}'),
                )
                await conn.execute("DELETE FROM chunks WHERE id = ?", (old_id,))

            await conn.execute(
                "UPDATE index_generations SET expected_chunks = (SELECT COUNT(*) FROM chunks) WHERE id = ?",
                (generation_id,),
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
