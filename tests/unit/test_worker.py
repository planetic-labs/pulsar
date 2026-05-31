import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.worker import Worker


@pytest.mark.asyncio
async def test_worker_has_pending_tasks(tmp_db, monkeypatch):
    monkeypatch.setattr("app.worker.get_sqlite_settings", lambda: tmp_db)
    worker = Worker()

    # 1. No tasks
    assert await worker._has_pending_tasks() is False

    # 2. Add a task
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
    from app.db import db_connection

    with db_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO tasks (task_type, payload, status) VALUES ('stage_1_download', ?, 'pending')",
            (json.dumps(payload),),
        )

    # Run consume stage (it should process 1 task and stop because we'll set
    # is_running=False inside mock or just check state)
    # Actually _consume_stage has a while is_running loop.

    # We'll run it in a background task and wait a bit
    task = asyncio.create_task(worker._consume_stage(["stage_1_download"]))

    await asyncio.sleep(0.5)
    worker.is_running = False  # Stop the loop
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
    mock_client.embed_batch_async = AsyncMock(return_value=[([0.1] * 1024, None)])

    worker = Worker()

    # Prepare DB: video + chunk + task
    from app.db import db_connection
    from app.repository import replace_chunks, replace_transcript, upsert_video

    with db_connection(tmp_db) as conn:
        vid = upsert_video(
            conn,
            source_type="t",
            source_file_id="f",
            title="T",
            source_url=None,
            mime_type=None,
            size_bytes=None,
            duration_sec=None,
            local_video_path=None,
            local_audio_path=None,
            processing_status="pending",
        )
        tid = replace_transcript(
            conn,
            video_id=vid,
            language="ru",
            confidence=1.0,
            raw_json_path=Path("r"),
            normalized_json_path=Path("n"),
        )
        replace_chunks(
            conn,
            video_id=vid,
            transcript_id=tid,
            chunks=[{"chunk_index": 0, "start_sec": 0, "end_sec": 1, "text": "Hello"}],
        )
        conn.execute(
            "INSERT INTO tasks (task_type, payload, status) VALUES ('stage_3_index', ?, 'pending')",
            (json.dumps({"video_id": vid}),),
        )

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


@pytest.mark.asyncio
async def test_worker_stage_2_missing_file_recovery(tmp_db, monkeypatch, mocker):
    monkeypatch.setattr("app.worker.get_sqlite_settings", lambda: tmp_db)

    # Mock settings and check_balance_threshold_async to pass
    mock_dg_settings = MagicMock()
    monkeypatch.setattr("app.worker.get_deepgram_settings", lambda: mock_dg_settings)

    # Mock DeepgramEngine check_balance_threshold_async to return (True, 10.0)
    mock_engine_cls = mocker.patch("app.worker.DeepgramEngine")
    mock_engine = mock_engine_cls.return_value
    mock_engine.check_balance_threshold_async = AsyncMock(return_value=(True, 10.0))

    # Mock transcribe_stage to raise FileNotFoundError
    mock_transcribe = mocker.patch("app.worker.transcribe_stage", new_callable=AsyncMock)
    mock_transcribe.side_effect = FileNotFoundError("Audio file not found")

    worker = Worker()

    # Insert stage 2 task
    payload = {"file_id": "drive_file_123", "audio_path": "/missing/file.wav", "title": "Missing File Video"}
    from app.db import db_connection

    with db_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO tasks (task_type, payload, status) VALUES ('stage_2_transcribe', ?, 'pending')",
            (json.dumps(payload),),
        )

    # Run stage 2 transcribe (task_id should be 1 because it's the first task in this fresh db)
    await worker._run_stage_2_transcribe(task_id=1, payload=payload)

    # Verify:
    # 1. The original stage_2_transcribe task (id=1) is deleted
    # 2. A new stage_1_download task is created with the correct payload
    with db_connection(tmp_db) as conn:
        old_task = conn.execute("SELECT * FROM tasks WHERE id = 1").fetchone()
        assert old_task is None

        new_task = conn.execute("SELECT * FROM tasks WHERE task_type = 'stage_1_download'").fetchone()
        assert new_task is not None
        assert new_task["status"] == "pending"

        new_payload = json.loads(new_task["payload"])
        assert new_payload["file_id"] == "drive_file_123"
        assert new_payload["title"] == "Missing File Video"


@pytest.mark.asyncio
async def test_worker_md5_duplicate_check(tmp_db, monkeypatch, mocker):
    monkeypatch.setattr("app.worker.get_sqlite_settings", lambda: tmp_db)

    # Mock settings
    mock_settings = MagicMock()
    monkeypatch.setattr("app.worker.get_google_drive_settings", lambda: mock_settings)

    # Mock GoogleDriveClient get_file
    mock_drive = MagicMock()
    mock_drive_file = MagicMock()
    mock_drive_file.md5_checksum = "duplicate-md5-hash"
    mock_drive.get_file = AsyncMock(return_value=mock_drive_file)
    mocker.patch("app.worker.GoogleDriveClient", return_value=mock_drive)

    # Mock download_and_extract_stage to ensure it is NOT called
    mock_download = mocker.patch("app.worker.download_and_extract_stage", new_callable=AsyncMock)

    # Prepare DB: Add existing video with the same MD5 checksum
    from app.db import db_connection
    from app.repository import upsert_video

    with db_connection(tmp_db) as conn:
        upsert_video(
            conn,
            source_type="google_drive",
            source_file_id="original_file_id",
            title="Original Video",
            source_url="http://original",
            mime_type="video/mp4",
            size_bytes=1000,
            duration_sec=60.0,
            local_video_path="/tmp/o.mp4",
            local_audio_path="/tmp/o.mp3",
            processing_status="indexed_chunks_ready",
            md5_checksum="duplicate-md5-hash",
        )

        # Insert a pending stage_1_download task for a different file ID but same content MD5
        payload = {"file_id": "new_duplicate_file_id", "title": "Duplicate Video"}
        conn.execute(
            "INSERT INTO tasks (task_type, payload, status) VALUES ('stage_1_download', ?, 'pending')",
            (json.dumps(payload),),
        )

    worker = Worker()

    with db_connection(tmp_db) as conn:
        task_id = conn.execute("SELECT id FROM tasks LIMIT 1").fetchone()["id"]

    await worker._run_stage_1_download(task_id=task_id, payload=payload)

    # Verify:
    # 1. download_and_extract_stage was NOT called
    mock_download.assert_not_called()

    # 2. The task status is updated to 'skipped_duplicate_md5'
    # 3. A new video entry exists in DB with is_md5_duplicate = 1 and status skipped_duplicate_md5
    with db_connection(tmp_db) as conn:
        task_row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert task_row["status"] == "skipped_duplicate_md5"

        video_row = conn.execute("SELECT * FROM videos WHERE source_file_id = 'new_duplicate_file_id'").fetchone()
        assert video_row is not None
        assert video_row["is_md5_duplicate"] == 1
        assert video_row["processing_status"] == "skipped_duplicate_md5"
        assert video_row["md5_checksum"] == "duplicate-md5-hash"
