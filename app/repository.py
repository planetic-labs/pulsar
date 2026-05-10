from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


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
    source_type: str,
    source_file_id: str | None,
    parent_folder_id: str | None = None,
    title: str,
    recorded_date: str | None = None,
    source_url: str | None,
    mime_type: str | None,
    size_bytes: int | None,
    duration_sec: float | None,
    local_video_path: str | None,
    local_audio_path: str | None,
    processing_status: str,
) -> int:
    source_id = source_file_id or ""
    # If recorded_date is not provided, try to extract it from title
    if recorded_date is None:
        recorded_date = extract_date_from_title(title)

    cursor = connection.execute(
        """
        INSERT INTO videos (
            source_type, source_file_id, parent_folder_id, title, recorded_date,
            source_url, mime_type, size_bytes, duration_sec,
            local_video_path, local_audio_path, processing_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source_type, source_file_id) DO UPDATE SET
            parent_folder_id = EXCLUDED.parent_folder_id,
            title = EXCLUDED.title,
            recorded_date = EXCLUDED.recorded_date,
            source_url = EXCLUDED.source_url,
            mime_type = EXCLUDED.mime_type,
            size_bytes = EXCLUDED.size_bytes,
            duration_sec = EXCLUDED.duration_sec,
            local_video_path = EXCLUDED.local_video_path,
            local_audio_path = EXCLUDED.local_audio_path,
            processing_status = EXCLUDED.processing_status,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (
            source_type,
            source_id,
            parent_folder_id,
            title,
            recorded_date,
            source_url,
            mime_type,
            size_bytes,
            duration_sec,
            local_video_path,
            local_audio_path,
            processing_status,
        ),
    )
    row = cursor.fetchone()
    return int(row["id"])


def replace_transcript(
    connection: sqlite3.Connection,
    *,
    video_id: int,
    language: str,
    confidence: float | None,
    raw_json_path: Path,
    normalized_json_path: Path,
) -> int:
    # Always delete the previous transcript for this video
    connection.execute("DELETE FROM transcripts WHERE video_id = ?", (video_id,))

    cursor = connection.execute(
        """
        INSERT INTO transcripts (
            video_id, language,
            confidence, raw_json_path, normalized_json_path
        ) VALUES (?, ?, ?, ?, ?)
        RETURNING id
        """,
        (video_id, language, confidence, str(raw_json_path), str(normalized_json_path)),
    )
    row = cursor.fetchone()
    return int(row["id"])


def check_transcript_exists(
    connection: sqlite3.Connection,
    video_id: int,
) -> bool:
    row = connection.execute("SELECT 1 FROM transcripts WHERE video_id = ?", (video_id,)).fetchone()
    return row is not None


def replace_chunks(
    connection: sqlite3.Connection,
    *,
    video_id: int,
    transcript_id: int,
    chunks: list[dict[str, Any]],
) -> None:
    connection.execute("DELETE FROM chunks WHERE transcript_id = ?", (transcript_id,))

    connection.executemany(
        """
        INSERT INTO chunks (
            video_id, transcript_id, chunk_index,
            start_sec, end_sec, text, speaker_tags
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                video_id,
                transcript_id,
                int(c["chunk_index"]),
                float(c["start_sec"]),
                float(c["end_sec"]),
                c["text"],
                c.get("speaker"),
            )
            for c in chunks
        ],
    )


def update_video_status(
    connection: sqlite3.Connection,
    *,
    video_id: int,
    processing_status: str,
    local_audio_path: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE videos
        SET processing_status = ?,
            local_audio_path = COALESCE(?, local_audio_path),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (processing_status, local_audio_path, video_id),
    )


def get_video_by_source_file_id(
    connection: sqlite3.Connection,
    *,
    source_type: str,
    source_file_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM videos WHERE source_type = ? AND source_file_id = ?", (source_type, source_file_id)
    ).fetchone()
    return dict(row) if row else None
