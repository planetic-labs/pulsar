from __future__ import annotations

import json
import sqlite3

import pytest

from app.config import SQLiteSettings
from app.db import db_connection, init_db
from app.indexing_state import enqueue_index_task
from app.repository import replace_chunks, upsert_video
from app.services.task_queue import TaskQueueService
from scripts.backup_manifest import create_manifest, validate_manifest


def test_replace_chunks_preserves_ids_and_records_outbox(tmp_path) -> None:
    settings = SQLiteSettings(tmp_path / "pulsar.db")
    with db_connection(settings) as conn:
        init_db(conn)
        video_id = upsert_video(
            conn,
            source_file_id="stable-source",
            title="Stable video",
            source_url=None,
            mime_type=None,
            size_bytes=None,
            duration_sec=None,
            status="transcribed",
        )
        first = [
            {"chunk_index": 0, "start_sec": 0, "end_sec": 5, "text": "first"},
            {"chunk_index": 1, "start_sec": 5, "end_sec": 10, "text": "second"},
        ]
        replace_chunks(conn, video_id=video_id, chunks=first)
        original_ids = {
            row["chunk_index"]: row["id"]
            for row in conn.execute("SELECT id, chunk_index FROM chunks WHERE video_id = ?", (video_id,))
        }

        replace_chunks(
            conn,
            video_id=video_id,
            chunks=[{"chunk_index": 0, "start_sec": 0, "end_sec": 6, "text": "first revised"}],
        )
        row = conn.execute("SELECT id, logical_id, content_hash FROM chunks WHERE video_id = ?", (video_id,)).fetchone()
        assert row["id"] == original_ids[0]
        assert row["logical_id"] and row["content_hash"]
        event_types = {event["event_type"] for event in conn.execute("SELECT event_type FROM index_outbox").fetchall()}
        assert event_types == {"delete", "upsert"}


def test_index_task_enqueue_is_idempotent(tmp_path) -> None:
    settings = SQLiteSettings(tmp_path / "pulsar.db")
    with db_connection(settings) as conn:
        init_db(conn)
        video_id = upsert_video(
            conn,
            source_file_id="dedupe-source",
            title="Dedupe video",
            source_url=None,
            mime_type=None,
            size_bytes=None,
            duration_sec=None,
            status="transcribed",
        )
        replace_chunks(
            conn,
            video_id=video_id,
            chunks=[{"chunk_index": 0, "start_sec": 0, "end_sec": 1, "text": "same"}],
        )
        first = enqueue_index_task(conn, video_id=video_id, title="Dedupe video")
        duplicate = enqueue_index_task(conn, video_id=video_id, title="Dedupe video")
        assert first is not None
        assert duplicate is None
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (first,))
        repeated_later = enqueue_index_task(conn, video_id=video_id, title="Updated metadata")
        assert repeated_later is not None
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_permanent_embedding_failure_opens_circuit(tmp_path) -> None:
    settings = SQLiteSettings(tmp_path / "pulsar.db")
    with db_connection(settings) as conn:
        init_db(conn)
        cursor = conn.execute(
            "INSERT INTO tasks (task_type, payload, status) VALUES ('stage_3_index', ?, 'running')",
            (json.dumps({"video_id": 1}),),
        )
        assert cursor.lastrowid is not None
        task_id = int(cursor.lastrowid)

    queue = TaskQueueService()
    queue.db_settings = settings
    await queue.fail_task(task_id, "403 Forbidden from embedding provider")

    with db_connection(settings) as conn:
        task = conn.execute(
            "SELECT status, failure_kind, next_attempt_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        state = json.loads(conn.execute("SELECT value FROM system_state WHERE key = 'circuit:embedding'").fetchone()[0])
        assert dict(task) == {"status": "failed", "failure_kind": "permanent", "next_attempt_at": None}
        assert state["state"] == "open"


@pytest.mark.asyncio
async def test_transient_failure_uses_sqlite_comparable_backoff(tmp_path) -> None:
    settings = SQLiteSettings(tmp_path / "pulsar.db")
    with db_connection(settings) as conn:
        init_db(conn)
        cursor = conn.execute(
            "INSERT INTO tasks (task_type, payload, status) VALUES ('stage_2_transcribe', '{}', 'running')"
        )
        assert cursor.lastrowid is not None
        task_id = int(cursor.lastrowid)

    queue = TaskQueueService()
    queue.db_settings = settings
    await queue.fail_task(task_id, "temporary network timeout", permanent=False)

    with db_connection(settings) as conn:
        task = conn.execute(
            "SELECT status, failure_kind, next_attempt_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert task["status"] == "pending"
        assert task["failure_kind"] == "transient"
        assert "T" not in task["next_attempt_at"]
        assert (
            conn.execute(
                "SELECT next_attempt_at > CURRENT_TIMESTAMP FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()[0]
            == 1
        )


def test_backup_manifest_detects_manticore_tampering(tmp_path) -> None:
    db_path = tmp_path / "pulsar.db"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
    backup_dir = tmp_path / "backup_manticore"
    table_dir = backup_dir / "backup-20260101000000" / "data" / "chunks"
    table_dir.mkdir(parents=True)
    (table_dir / "chunks.meta").write_bytes(b"consistent-index")
    manifest_path = tmp_path / "manifest.json"
    manifest = create_manifest(db_path, backup_dir, manticore_count=0, environment="dev")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    validate_manifest(db_path, manifest_path, backup_dir, environment="dev")
    (table_dir / "chunks.meta").write_bytes(b"tampered-index")
    with pytest.raises(ValueError, match="Manticore backup SHA-256 mismatch"):
        validate_manifest(db_path, manifest_path, backup_dir, environment="dev")
