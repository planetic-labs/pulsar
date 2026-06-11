from app.repository import (
    get_video_by_source_file_id,
    replace_chunks,
    update_video_status,
    upsert_folder,
    upsert_video,
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
        source_file_id="v1",
        title="Test Video",
        source_url=None,
        mime_type="video/mp4",
        size_bytes=1000,
        duration_sec=60.0,
        status="pending",
    )
    assert video_id > 0

    video = get_video_by_source_file_id(mock_db_conn, source_file_id="v1")
    assert video is not None
    assert video["title"] == "Test Video"

    # Test update
    upsert_video(
        mock_db_conn,
        source_file_id="v1",
        title="Updated Title",
        source_url=None,
        mime_type="video/mp4",
        size_bytes=1000,
        duration_sec=60.0,
        status="downloading",
    )
    video = get_video_by_source_file_id(mock_db_conn, source_file_id="v1")
    assert video is not None
    assert video["title"] == "Updated Title"
    assert video["status"] == "downloading"


def test_replace_chunks(mock_db_conn):
    video_id = upsert_video(
        mock_db_conn,
        source_file_id="v2",
        title="V2",
        source_url=None,
        mime_type=None,
        size_bytes=None,
        duration_sec=None,
        status="pending",
    )

    chunks = [
        {"chunk_index": 0, "start_sec": 0, "end_sec": 5, "text": "Chunk 1", "speaker": "A"},
        {"chunk_index": 1, "start_sec": 5, "end_sec": 10, "text": "Chunk 2", "speaker": "B"},
    ]
    replace_chunks(mock_db_conn, video_id=video_id, chunks=chunks)

    rows = mock_db_conn.execute("SELECT * FROM chunks WHERE video_id=?", (video_id,)).fetchall()
    assert len(rows) == 2
    assert rows[0]["text"] == "Chunk 1"


def test_update_video_status(mock_db_conn):
    video_id = upsert_video(
        mock_db_conn,
        source_file_id="v3",
        title="V3",
        source_url=None,
        mime_type=None,
        size_bytes=None,
        duration_sec=None,
        status="pending",
    )

    update_video_status(mock_db_conn, video_id=video_id, status="completed")

    video = get_video_by_source_file_id(mock_db_conn, source_file_id="v3")
    assert video is not None
    assert video["status"] == "completed"
