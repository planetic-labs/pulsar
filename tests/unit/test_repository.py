import sqlite3
import pytest
from pathlib import Path
from app.repository import (
    upsert_folder,
    upsert_video,
    replace_transcript,
    replace_chunks,
    update_video_status,
    get_video_by_source_file_id,
)

def test_upsert_folder(mock_db_conn):
    upsert_folder(mock_db_conn, folder_id="f1", name="Test Folder")
    row = mock_db_conn.execute("SELECT * FROM folders WHERE id='f1'").fetchone()
    assert row["name"] == "Test Folder"
    
    upsert_folder(mock_db_conn, folder_id="f1", name="Updated Folder")
    row = mock_db_conn.execute("SELECT * FROM folders WHERE id='f1'").fetchone()
    assert row["name"] == "Updated Folder"

def test_upsert_video(mock_db_conn):
    video_id = upsert_video(
        mock_db_conn,
        source_type="google_drive",
        source_file_id="v1",
        title="Test Video",
        source_url=None,
        mime_type="video/mp4",
        size_bytes=1000,
        duration_sec=60.0,
        local_video_path=None,
        local_audio_path=None,
        processing_status="pending"
    )
    assert video_id > 0
    
    video = get_video_by_source_file_id(mock_db_conn, source_type="google_drive", source_file_id="v1")
    assert video["title"] == "Test Video"
    
    # Test update
    upsert_video(
        mock_db_conn,
        source_type="google_drive",
        source_file_id="v1",
        title="Updated Title",
        source_url=None,
        mime_type="video/mp4",
        size_bytes=1000,
        duration_sec=60.0,
        local_video_path=None,
        local_audio_path=None,
        processing_status="downloading"
    )
    video = get_video_by_source_file_id(mock_db_conn, source_type="google_drive", source_file_id="v1")
    assert video["title"] == "Updated Title"
    assert video["processing_status"] == "downloading"

def test_replace_transcript_and_chunks(mock_db_conn):
    video_id = upsert_video(
        mock_db_conn, source_type="test", source_file_id="v2", title="V2",
        source_url=None, mime_type=None, size_bytes=None, duration_sec=None,
        local_video_path=None, local_audio_path=None, processing_status="pending"
    )
    
    transcript_id = replace_transcript(
        mock_db_conn,
        video_id=video_id,
        language="ru",
        confidence=0.95,
        raw_json_path=Path("raw.json"),
        normalized_json_path=Path("norm.json")
    )
    assert transcript_id > 0
    
    chunks = [
        {"chunk_index": 0, "start_sec": 0, "end_sec": 5, "text": "Chunk 1", "speaker": "A"},
        {"chunk_index": 1, "start_sec": 5, "end_sec": 10, "text": "Chunk 2", "speaker": "B"},
    ]
    replace_chunks(mock_db_conn, video_id=video_id, transcript_id=transcript_id, chunks=chunks)
    
    rows = mock_db_conn.execute("SELECT * FROM chunks WHERE transcript_id=?", (transcript_id,)).fetchall()
    assert len(rows) == 2
    assert rows[0]["text"] == "Chunk 1"
    assert rows[1]["speaker_tags"] == "B"

def test_update_video_status(mock_db_conn):
    video_id = upsert_video(
        mock_db_conn, source_type="test", source_file_id="v3", title="V3",
        source_url=None, mime_type=None, size_bytes=None, duration_sec=None,
        local_video_path=None, local_audio_path=None, processing_status="pending"
    )
    
    update_video_status(mock_db_conn, video_id=video_id, processing_status="completed", local_audio_path="/path/to/audio")
    
    video = get_video_by_source_file_id(mock_db_conn, source_type="test", source_file_id="v3")
    assert video["processing_status"] == "completed"
    assert video["local_audio_path"] == "/path/to/audio"
