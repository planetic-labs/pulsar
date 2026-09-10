from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import Database


@pytest.mark.asyncio
async def test_init_schema_migrates_legacy_videos_before_creating_schema_objects(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file_id TEXT NOT NULL UNIQUE,
                parent_folder_id TEXT,
                md5_checksum TEXT,
                title TEXT NOT NULL,
                recorded_date DATE,
                is_short BOOLEAN DEFAULT FALSE,
                source_url TEXT,
                mime_type TEXT,
                size_bytes BIGINT,
                duration_sec DOUBLE PRECISION,
                status TEXT NOT NULL,
                is_4k BOOLEAN DEFAULT FALSE,
                is_missing BOOLEAN DEFAULT FALSE,
                is_excluded BOOLEAN DEFAULT FALSE,
                is_silent BOOLEAN DEFAULT FALSE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(db_path)
    await db.connect()
    try:
        await db.init_schema()

        async with db.transaction() as conn:
            async with conn.execute("PRAGMA table_info(videos)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            async with conn.execute("PRAGMA index_list(videos)") as cursor:
                indexes = {row[1] for row in await cursor.fetchall()}

        assert "original_id" in columns
        assert "idx_videos_original_id" in indexes
        assert "uidx_videos_md5_original" in indexes
    finally:
        await db.close()
