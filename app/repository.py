from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from app.chunking import CHUNKING_ALGORITHM_VERSION
from app.indexing_state import (
    add_outbox_event,
    chunk_content_hash,
    ensure_active_generation,
    stable_chunk_logical_id,
)
from app.manticore import models


def get_cached_embedding(
    connection: sqlite3.Connection, query: str
) -> tuple[list[float], models.SparseVector | None] | None:
    """Retrieves cached embedding from SQLite if exists."""
    row = connection.execute(
        "SELECT dense_vector, sparse_indices, sparse_values FROM query_cache WHERE query = ?", (query,)
    ).fetchone()
    if not row:
        return None

    dense = json.loads(row["dense_vector"])
    sparse = None
    if row["sparse_indices"] and row["sparse_values"]:
        sparse = models.SparseVector(indices=json.loads(row["sparse_indices"]), values=json.loads(row["sparse_values"]))
    return dense, sparse


def save_cached_embedding(
    connection: sqlite3.Connection, query: str, dense: list[float], sparse: models.SparseVector | None
) -> None:
    """Saves embedding to SQLite cache."""
    s_indices = json.dumps(sparse.indices) if sparse else None
    s_values = json.dumps(sparse.values) if sparse else None
    connection.execute(
        """
        INSERT OR REPLACE INTO query_cache (query, dense_vector, sparse_indices, sparse_values)
        VALUES (?, ?, ?, ?)
        """,
        (query, json.dumps(dense), s_indices, s_values),
    )


def extract_date_from_title(title: str) -> str | None:
    """
    Extracts date from the BEGINNING of the title.
    Supports:
    - YYYY.MM.DD
    - DD.MM.YYYY
    - YY.MM.DD
    Returns date in ISO format YYYY-MM-DD or None.
    """
    t = title.strip()

    # 1. Try YYYY.MM.DD
    m1 = re.match(r"^(\d{4})\.(\d{2})\.(\d{2})\b", t)
    if m1:
        y, m, d = map(int, m1.groups())
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    # 2. Try DD.MM.YYYY
    m2 = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})\b", t)
    if m2:
        d, m, y = map(int, m2.groups())
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    # 3. Try YY.MM.DD
    m3 = re.match(r"^(\d{2})\.(\d{2})\.(\d{2})\b", t)
    if m3:
        y, m, d = map(int, m3.groups())
        # Assume 20xx for 2-digit year (consistent with YY.MM.DD requirement)
        y = 2000 + y
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    return None


def upsert_folder(
    connection: sqlite3.Connection,
    *,
    folder_id: str,
    name: str,
    parent_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO folders (id, name, parent_id)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = EXCLUDED.name,
            parent_id = EXCLUDED.parent_id
        """,
        (folder_id, name, parent_id),
    )


def upsert_video(
    connection: sqlite3.Connection,
    *,
    source_file_id: str | None,
    parent_folder_id: str | None = None,
    md5_checksum: str | None = None,
    title: str,
    recorded_date: str | None = None,
    source_url: str | None,
    mime_type: str | None,
    size_bytes: int | None,
    duration_sec: float | None,
    is_short: bool | None = None,
    status: str,
    is_4k: bool | None = None,
) -> int:
    source_id = source_file_id or ""
    # If recorded_date is not provided, try to extract it from title
    if recorded_date is None:
        recorded_date = extract_date_from_title(title)

    if is_short is None:
        is_short = bool(duration_sec and duration_sec <= 1800)

    if is_4k is None:
        is_4k = bool(re.search(r"4[KК]", title))

    cursor = connection.execute(
        """
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
        """,
        (
            source_id,
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
    )
    row = cursor.fetchone()
    return int(row["id"])


def replace_chunks(
    connection: sqlite3.Connection,
    *,
    video_id: int,
    chunks: list[dict[str, Any]],
) -> None:
    """Upsert a chunk set atomically while preserving stable row IDs."""
    video = connection.execute("SELECT source_file_id FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not video:
        raise ValueError(f"Video {video_id} does not exist")

    generation_id = ensure_active_generation(connection)
    existing = {
        int(row["chunk_index"]): {"content_hash": row["content_hash"], "id": int(row["id"])}
        for row in connection.execute(
            "SELECT id, chunk_index, content_hash FROM chunks WHERE video_id = ?",
            (video_id,),
        ).fetchall()
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
        cursor = connection.execute(
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
        )
        row = cursor.fetchone()
        assert row is not None
        add_outbox_event(
            connection,
            event_type="upsert",
            video_id=video_id,
            chunk_id=int(row["id"]),
            generation_id=generation_id,
            event_version=content_hash,
        )

    for chunk_index, old in existing.items():
        if chunk_index in retained_indices:
            continue
        old_id = int(old["id"])
        event_version = str(old["content_hash"] or "legacy")
        add_outbox_event(
            connection,
            event_type="delete",
            video_id=video_id,
            chunk_id=old_id,
            generation_id=generation_id,
            event_version=event_version,
        )
        connection.execute("DELETE FROM chunks WHERE id = ?", (old_id,))

    connection.execute(
        "UPDATE index_generations SET expected_chunks = (SELECT COUNT(*) FROM chunks) WHERE id = ?",
        (generation_id,),
    )


def update_video_status(
    connection: sqlite3.Connection,
    *,
    video_id: int,
    status: str,
) -> None:
    connection.execute(
        """
        UPDATE videos
        SET status = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, video_id),
    )


def get_video_by_source_file_id(
    connection: sqlite3.Connection,
    *,
    source_file_id: str,
) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM videos WHERE source_file_id = ?", (source_file_id,)).fetchone()
    return dict(row) if row else None
