from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import psycopg

def upsert_video(
    connection: psycopg.Connection,
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
    row = connection.execute(
        """
        INSERT INTO videos (
            source_type, source_file_id, title, source_url, 
            mime_type, size_bytes, duration_sec, 
            local_video_path, local_audio_path, processing_status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    ).fetchone()
    
    return int(row["id"])

def replace_transcript(
    connection: psycopg.Connection,
    *,
    video_id: int,
    engine: str,
    language: str,
    transcript_text: str,
    confidence: float | None,
    raw_json_path: Path,
    normalized_json_path: Path,
    is_primary: bool = False,
) -> int:
    # If this is a primary transcript, unset any other primary for this video
    if is_primary:
        connection.execute(
            "UPDATE transcripts SET is_primary = FALSE WHERE video_id = %s",
            (video_id,)
        )
    
    # Only delete the previous transcript for the SAME engine for this video
    connection.execute(
        "DELETE FROM transcripts WHERE video_id = %s AND engine = %s",
        (video_id, engine)
    )
    
    row = connection.execute(
        """
        INSERT INTO transcripts (
            video_id, engine, language, transcript_text, 
            confidence, raw_json_path, normalized_json_path, is_primary
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (video_id, engine, language, transcript_text, 
         confidence, str(raw_json_path), str(normalized_json_path), is_primary)
    ).fetchone()
    return int(row["id"])

def replace_chunks(
    connection: psycopg.Connection,
    *,
    video_id: int,
    transcript_id: int,
    chunks: list[dict[str, Any]],
) -> None:
    connection.execute("DELETE FROM chunks WHERE transcript_id = %s", (transcript_id,))
    
    # We use executemany for efficiency
    with connection.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunks (
                video_id, transcript_id, chunk_index, 
                start_sec, end_sec, text, embedding
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    video_id,
                    transcript_id,
                    int(c["chunk_index"]),
                    float(c["start_sec"]),
                    float(c["end_sec"]),
                    c["text"],
                    c.get("embedding") # This can be a list/ndarray or None
                )
                for c in chunks
            ]
        )

def update_video_status(
    connection: psycopg.Connection,
    *,
    video_id: int,
    processing_status: str,
    local_audio_path: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE videos
        SET processing_status = %s,
            local_audio_path = COALESCE(%s, local_audio_path),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (processing_status, local_audio_path, video_id),
    )

def get_video_by_source_file_id(
    connection: psycopg.Connection,
    *,
    source_type: str,
    source_file_id: str,
) -> dict[str, Any] | None:
    return connection.execute(
        "SELECT * FROM videos WHERE source_type = %s AND source_file_id = %s",
        (source_type, source_file_id)
    ).fetchone()

def check_transcript_exists(
    connection: psycopg.Connection,
    video_id: int,
    engine: str,
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM transcripts WHERE video_id = %s AND engine = %s",
        (video_id, engine)
    ).fetchone()
    return row is not None
