from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import sqlite3

def upsert_video(
    connection: sqlite3.Connection,
    *,
    source_type: str,
    source_file_id: str | None,
    title: str,
    source_url: str | None,
    mime_type: str | None,
    size_bytes: int | None,
    duration_sec: float | None,
    local_video_path: str | None,
    local_audio_path: str | None,
    processing_status: str,
) -> int:
    source_id = source_file_id or ""
    cursor = connection.execute(
        """
        INSERT INTO videos (
            source_type, source_file_id, title, source_url, 
            mime_type, size_bytes, duration_sec, 
            local_video_path, local_audio_path, processing_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source_type, source_file_id) DO UPDATE SET
            title = EXCLUDED.title,
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
        (source_type, source_id, title, source_url, 
         mime_type, size_bytes, duration_sec, 
         local_video_path, local_audio_path, processing_status)
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
        (video_id, language, confidence, str(raw_json_path), str(normalized_json_path))
    )
    row = cursor.fetchone()
    return int(row["id"])

def check_transcript_exists(
    connection: sqlite3.Connection,
    video_id: int,
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM transcripts WHERE video_id = ?",
        (video_id,)
    ).fetchone()
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
                c.get("speaker") 
            )
            for c in chunks
        ]
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
        "SELECT * FROM videos WHERE source_type = ? AND source_file_id = ?",
        (source_type, source_file_id)
    ).fetchone()
    return dict(row) if row else None
