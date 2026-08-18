from __future__ import annotations

import re

from app.database import Database

# Извлечение даты из названия
DATE_REGEXES = [
    r"^(\d{4})\.(\d{2})\.(\d{2})\b",  # YYYY.MM.DD
    r"^(\d{2})\.(\d{2})\.(\d{4})\b",  # DD.MM.YYYY
    r"^(\d{2})\.(\d{2})\.(\d{2})\b",  # YY.MM.DD
]


def extract_date_from_title(title: str) -> str | None:
    """Извлекает дату в формате YYYY-MM-DD из начала названия файла."""
    t = title.strip()

    # 1. YYYY.MM.DD
    m1 = re.match(DATE_REGEXES[0], t)
    if m1:
        y, m, d = map(int, m1.groups())
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    # 2. DD.MM.YYYY
    m2 = re.match(DATE_REGEXES[1], t)
    if m2:
        d, m, y = map(int, m2.groups())
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    # 3. YY.MM.DD
    m3 = re.match(DATE_REGEXES[2], t)
    if m3:
        y, m, d = map(int, m3.groups())
        y = 2000 + y
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    return None


class VideoRepository:
    """Репозиторий для работы с таблицей videos."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_by_id(self, video_id: int) -> dict[str, str | int | float | bool | None] | None:
        async with (
            self.db.transaction() as conn,
            conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)) as cursor,
        ):
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_by_source_file_id(self, source_file_id: str) -> dict[str, str | int | float | bool | None] | None:
        async with (
            self.db.transaction() as conn,
            conn.execute("SELECT * FROM videos WHERE source_file_id = ?", (source_file_id,)) as cursor,
        ):
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def upsert(
        self,
        *,
        source_file_id: str,
        parent_folder_id: str | None = None,
        md5_checksum: str | None = None,
        title: str,
        recorded_date: str | None = None,
        source_url: str | None = None,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        duration_sec: float | None = None,
        is_short: bool | None = None,
        status: str,
        is_4k: bool | None = None,
    ) -> int:
        if recorded_date is None:
            recorded_date = extract_date_from_title(title)

        if is_short is None:
            is_short = bool(duration_sec and duration_sec <= 1800)

        if is_4k is None:
            is_4k = bool(re.search(r"4[KК]", title))

        sql = """
            INSERT INTO videos (
                source_file_id, parent_folder_id, md5_checksum, title, recorded_date,
                is_short, source_url, mime_type, size_bytes, duration_sec,
                status, is_4k
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_file_id) DO UPDATE SET
                parent_folder_id = EXCLUDED.parent_folder_id,
                md5_checksum = EXCLUDED.md5_checksum,
                title = EXCLUDED.title,
                recorded_date = EXCLUDED.recorded_date,
                is_short = EXCLUDED.is_short,
                source_url = EXCLUDED.source_url,
                mime_type = EXCLUDED.mime_type,
                size_bytes = EXCLUDED.size_bytes,
                duration_sec = EXCLUDED.duration_sec,
                status = EXCLUDED.status,
                is_4k = EXCLUDED.is_4k,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id
        """
        async with (
            self.db.transaction() as conn,
            conn.execute(
                sql,
                (
                    source_file_id,
                    parent_folder_id,
                    md5_checksum,
                    title,
                    recorded_date,
                    is_short,
                    source_url,
                    mime_type,
                    size_bytes,
                    duration_sec,
                    status,
                    is_4k,
                ),
            ) as cursor,
        ):
            row = await cursor.fetchone()
            assert row is not None
            return int(row["id"])

    async def update_status(self, video_id: int, status: str) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE videos SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, video_id),
            )

    async def update(self, video_id: int, **kwargs: str | int | float | bool | None) -> None:
        if not kwargs:
            return
        fields = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values())
        values.append(video_id)
        sql = f"UPDATE videos SET {fields}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
        async with self.db.transaction() as conn:
            await conn.execute(sql, values)

    async def get_metadata_batch(self, video_ids: set[int]) -> dict[int, dict[str, str | int | float | bool | None]]:
        if not video_ids:
            return {}
        placeholders = ",".join(["?"] * len(video_ids))
        sql = f"SELECT * FROM videos WHERE id IN ({placeholders})"
        result = {}
        async with self.db.transaction() as conn, conn.execute(sql, list(video_ids)) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                result[row["id"]] = dict(row)
        return result

    async def get_all(self) -> list[dict[str, str | int | float | bool | None]]:
        async with (
            self.db.transaction() as conn,
            conn.execute("SELECT id, title FROM videos ORDER BY title ASC") as cursor,
        ):
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
