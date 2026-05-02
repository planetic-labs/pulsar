import json
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from app.worker import Worker

@pytest.mark.asyncio
async def test_worker_has_pending_tasks(tmp_db, monkeypatch):
    monkeypatch.setattr("app.worker.get_sqlite_settings", lambda: tmp_db)
    worker = Worker()
    
    # 1. No tasks
    assert await worker._has_pending_tasks() is False
    
    # 2. Add a task
    import sqlite3
    from app.db import db_connection
    with db_connection(tmp_db) as conn:
        conn.execute("INSERT INTO tasks (task_type, payload, status) VALUES ('t', '{}', 'pending')")
    
    assert await worker._has_pending_tasks() is True

@pytest.mark.asyncio
async def test_worker_consume_stage_lifecycle(tmp_db, monkeypatch, mocker):
    monkeypatch.setattr("app.worker.get_sqlite_settings", lambda: tmp_db)
    worker = Worker()
    worker.is_running = True
    
    # Mock stage 1 implementation
    mock_stage_1 = mocker.patch("app.worker.download_and_extract_stage", new_callable=AsyncMock)
    mock_stage_1.return_value = {"title": "Test Video", "audio_path": "/tmp/a.mp3"}
    
    # Insert task
    payload = {"file_id": "file1"}
    import sqlite3
    from app.db import db_connection
    with db_connection(tmp_db) as conn:
        conn.execute("INSERT INTO tasks (task_type, payload, status) VALUES ('stage_1_download', ?, 'pending')", (json.dumps(payload),))
    
    # Run consume stage (it should process 1 task and stop because we'll set is_running=False inside mock or just check state)
    # Actually _consume_stage has a while is_running loop.
    
    # We'll run it in a background task and wait a bit
    task = asyncio.create_task(worker._consume_stage(["stage_1_download"]))
    
    await asyncio.sleep(0.5)
    worker.is_running = False # Stop the loop
    await task
    
    # Check if task was processed
    with db_connection(tmp_db) as conn:
        row = conn.execute("SELECT status, task_type FROM tasks WHERE id=1").fetchone()
        # Should be moved to stage 2
        assert row["status"] == "pending"
        assert row["task_type"] == "stage_2_transcribe"
    
    mock_stage_1.assert_called_once()

@pytest.mark.asyncio
async def test_worker_stage_3_index(tmp_db, monkeypatch, mocker, mock_qdrant):
    monkeypatch.setattr("app.worker.get_sqlite_settings", lambda: tmp_db)
    monkeypatch.setattr("app.worker.get_qdrant_settings", lambda: MagicMock(collection_name="test_col"))
    monkeypatch.setattr("app.worker.get_qdrant_client", lambda: mock_qdrant)
    
    # Mock settings to return real strings just in case
    mock_settings = MagicMock()
    mock_settings.api_url = "http://test-api"
    mock_settings.model_id = "test-model"
    monkeypatch.setattr("app.worker.get_embedding_settings", lambda: mock_settings)
    
    # Mock the client class in app.worker
    mock_client_cls = mocker.patch("app.worker.UnifiedEmbeddingClient", autospec=True)
    mock_client = mock_client_cls.return_value
    mock_client.embed_batch_async = AsyncMock(return_value=[([0.1]*1024, None)])
    
    worker = Worker()
    
    # Prepare DB: video + chunk + task
    from app.repository import upsert_video, replace_transcript, replace_chunks
    from app.db import db_connection
    with db_connection(tmp_db) as conn:
        vid = upsert_video(conn, source_type="t", source_file_id="f", title="T", source_url=None, mime_type=None, size_bytes=None, duration_sec=None, local_video_path=None, local_audio_path=None, processing_status="pending")
        tid = replace_transcript(conn, video_id=vid, language="ru", confidence=1.0, raw_json_path="r", normalized_json_path="n")
        replace_chunks(conn, video_id=vid, transcript_id=tid, chunks=[{"chunk_index": 0, "start_sec": 0, "end_sec": 1, "text": "Hello"}])
        conn.execute("INSERT INTO tasks (task_type, payload, status) VALUES ('stage_3_index', ?, 'pending')", (json.dumps({"video_id": vid}),))

    # Run stage 3
    await worker._run_stage_3_index(task_id=1, payload={"video_id": vid})
    
    # Verify Qdrant call
    assert mock_qdrant.upsert.called
    
    # Verify task completed
    with db_connection(tmp_db) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=1").fetchone()
        assert row["status"] == "completed"
        v_row = conn.execute("SELECT processing_status FROM videos WHERE id=?", (vid,)).fetchone()
        assert v_row["processing_status"] == "indexed_chunks_ready"
