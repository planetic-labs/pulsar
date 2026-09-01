from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.chunking import CHUNKING_ALGORITHM_VERSION, get_chunking_config_hash
from app.config import get_embedding_settings, get_manticore_settings


class IndexConfigurationMismatchError(RuntimeError):
    """Raised when runtime indexing settings differ from the active generation."""


def stable_chunk_logical_id(source_file_id: str, chunk_index: int) -> str:
    """Return a stable identity that survives SQLite row replacement and restore."""
    raw = f"{source_file_id}:{chunk_index}".encode()
    return hashlib.sha256(raw).hexdigest()


def chunk_content_hash(*, text: str, start_sec: float, end_sec: float) -> str:
    """Hash all searchable chunk content and boundaries deterministically."""
    payload = json.dumps(
        {"end_sec": float(end_sec), "start_sec": float(start_sec), "text": text.strip()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def embedding_config_hash() -> str:
    settings = get_embedding_settings()
    payload = json.dumps(
        {
            "dimension": settings.dimension,
            "model": settings.model_id,
            "provider": settings.provider,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def generation_name() -> str:
    identity = f"{get_chunking_config_hash()}:{embedding_config_hash()}"
    return f"index-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"


def ensure_active_generation(conn: sqlite3.Connection) -> int:
    """Create the initial generation or verify that runtime config still matches it."""
    embedding = get_embedding_settings()
    manticore = get_manticore_settings()
    config_hash = get_chunking_config_hash()
    row = conn.execute(
        """
        SELECT id, config_hash, embedding_model, embedding_dimension
        FROM index_generations WHERE status = 'active'
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if row:
        if (
            row["config_hash"] != config_hash
            or row["embedding_model"] != embedding.model_id
            or int(row["embedding_dimension"]) != embedding.dimension
        ):
            raise IndexConfigurationMismatchError(
                "Runtime chunking/embedding settings differ from the active index generation; "
                "build and validate a new generation before indexing"
            )
        return int(row["id"])

    expected_chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    cursor = conn.execute(
        """
        INSERT INTO index_generations (
            name, status, chunking_version, config_hash,
            embedding_model, embedding_dimension, manticore_table,
            expected_chunks, indexed_chunks, activated_at
        ) VALUES (?, 'active', ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
        """,
        (
            generation_name(),
            CHUNKING_ALGORITHM_VERSION,
            config_hash,
            embedding.model_id,
            embedding.dimension,
            manticore.table_name,
            expected_chunks,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


async def ensure_active_generation_async(conn: Any) -> int:
    """Async counterpart used by repository services sharing an aiosqlite transaction."""
    embedding = get_embedding_settings()
    manticore = get_manticore_settings()
    config_hash = get_chunking_config_hash()
    async with conn.execute(
        """
        SELECT id, config_hash, embedding_model, embedding_dimension
        FROM index_generations WHERE status = 'active'
        ORDER BY id DESC LIMIT 1
        """
    ) as cursor:
        row = await cursor.fetchone()
    if row:
        if (
            row["config_hash"] != config_hash
            or row["embedding_model"] != embedding.model_id
            or int(row["embedding_dimension"]) != embedding.dimension
        ):
            raise IndexConfigurationMismatchError(
                "Runtime chunking/embedding settings differ from the active index generation"
            )
        return int(row["id"])

    async with conn.execute("SELECT COUNT(*) FROM chunks") as cursor:
        count_row = await cursor.fetchone()
    expected_chunks = int(count_row[0])
    cursor = await conn.execute(
        """
        INSERT INTO index_generations (
            name, status, chunking_version, config_hash,
            embedding_model, embedding_dimension, manticore_table,
            expected_chunks, indexed_chunks, activated_at
        ) VALUES (?, 'active', ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
        """,
        (
            generation_name(),
            CHUNKING_ALGORITHM_VERSION,
            config_hash,
            embedding.model_id,
            embedding.dimension,
            manticore.table_name,
            expected_chunks,
        ),
    )
    return int(cursor.lastrowid)


def backfill_chunk_metadata(conn: sqlite3.Connection, generation_id: int) -> int:
    """Backfill reliability fields without changing primary keys or chunk content."""
    rows = conn.execute(
        """
        SELECT c.id, c.chunk_index, c.text, c.start_sec, c.end_sec, v.source_file_id
        FROM chunks c JOIN videos v ON v.id = c.video_id
        WHERE c.logical_id IS NULL OR c.content_hash IS NULL OR c.generation_id IS NULL
        """
    ).fetchall()
    updates = [
        (
            stable_chunk_logical_id(str(row["source_file_id"]), int(row["chunk_index"])),
            chunk_content_hash(
                text=str(row["text"]),
                start_sec=float(row["start_sec"]),
                end_sec=float(row["end_sec"]),
            ),
            generation_id,
            int(row["id"]),
        )
        for row in rows
    ]
    if updates:
        conn.executemany(
            """
            UPDATE chunks
            SET logical_id = COALESCE(logical_id, ?),
                content_hash = COALESCE(content_hash, ?),
                generation_id = COALESCE(generation_id, ?)
            WHERE id = ?
            """,
            updates,
        )
    return len(updates)


def chunk_set_hash(conn: sqlite3.Connection, video_id: int) -> str:
    rows = conn.execute(
        "SELECT logical_id, content_hash FROM chunks WHERE video_id = ? ORDER BY chunk_index",
        (video_id,),
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(f"{row['logical_id']}:{row['content_hash']}\n".encode())
    return digest.hexdigest()


def index_task_dedupe_key(conn: sqlite3.Connection, video_id: int, generation_id: int) -> str:
    return f"stage_3_index:{generation_id}:{video_id}:{chunk_set_hash(conn, video_id)}"


def enqueue_index_task(
    conn: sqlite3.Connection,
    *,
    video_id: int,
    title: str,
    priority: int = 5,
    generation_id: int | None = None,
) -> int | None:
    """Queue one idempotent indexing task for an exact chunk set."""
    resolved_generation = generation_id or ensure_active_generation(conn)
    dedupe_key = index_task_dedupe_key(conn, video_id, resolved_generation)
    payload = json.dumps(
        {"generation_id": resolved_generation, "title": title, "video_id": video_id},
        ensure_ascii=False,
        sort_keys=True,
    )
    cursor = conn.execute(
        """
        INSERT INTO tasks (
            video_id, task_type, payload, status, priority, generation_id, dedupe_key
        ) VALUES (?, 'stage_3_index', ?, 'pending', ?, ?, ?)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (video_id, payload, priority, resolved_generation, dedupe_key),
    )
    row = cursor.fetchone()
    return int(row["id"]) if row else None


async def enqueue_index_task_async(
    conn: Any,
    *,
    video_id: int,
    title: str,
    priority: int = 5,
    generation_id: int | None = None,
) -> int | None:
    """Async idempotent index enqueue for aiosqlite transactions."""
    resolved_generation = generation_id or await ensure_active_generation_async(conn)
    async with conn.execute(
        "SELECT logical_id, content_hash FROM chunks WHERE video_id = ? ORDER BY chunk_index",
        (video_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(f"{row['logical_id']}:{row['content_hash']}\n".encode())
    dedupe_key = f"stage_3_index:{resolved_generation}:{video_id}:{digest.hexdigest()}"
    payload = json.dumps(
        {"generation_id": resolved_generation, "title": title, "video_id": video_id},
        ensure_ascii=False,
        sort_keys=True,
    )
    async with conn.execute(
        """
        INSERT INTO tasks (
            video_id, task_type, payload, status, priority, generation_id, dedupe_key
        ) VALUES (?, 'stage_3_index', ?, 'pending', ?, ?, ?)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (video_id, payload, priority, resolved_generation, dedupe_key),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row["id"]) if row else None


def add_outbox_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    video_id: int,
    chunk_id: int,
    generation_id: int,
    event_version: str,
) -> None:
    event_key = f"{event_type}:{generation_id}:{chunk_id}:{event_version}"
    payload = json.dumps({"chunk_id": chunk_id, "video_id": video_id}, sort_keys=True)
    conn.execute(
        """
        INSERT INTO index_outbox (
            event_key, event_type, video_id, chunk_id, generation_id, payload
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key) DO NOTHING
        """,
        (event_key, event_type, video_id, chunk_id, generation_id, payload),
    )


def get_pending_video_outbox_event_ids(conn: sqlite3.Connection, video_id: int, generation_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT id FROM index_outbox
        WHERE video_id = ? AND generation_id = ? AND status IN ('pending', 'processing')
        ORDER BY id
        """,
        (video_id, generation_id),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def mark_outbox_events_completed(conn: sqlite3.Connection, event_ids: list[int]) -> None:
    """Complete only the exact events included in an indexing snapshot."""
    if not event_ids:
        return
    placeholders = ",".join("?" for _ in event_ids)
    conn.execute(
        f"""
        UPDATE index_outbox
        SET status = 'completed', error_message = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id IN ({placeholders}) AND status IN ('pending', 'processing')
        """,
        event_ids,
    )


def get_pending_deleted_chunk_ids(conn: sqlite3.Connection, video_id: int, generation_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT chunk_id FROM index_outbox
        WHERE video_id = ? AND generation_id = ? AND event_type = 'delete'
          AND status IN ('pending', 'processing') AND chunk_id IS NOT NULL
        ORDER BY id
        """,
        (video_id, generation_id),
    ).fetchall()
    return [int(row["chunk_id"]) for row in rows]


def set_system_state(conn: sqlite3.Connection, key: str, value: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO system_state (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """,
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
    )


def get_system_state(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def open_embedding_circuit(conn: sqlite3.Connection, error: str, *, permanent: bool) -> None:
    set_system_state(
        conn,
        "circuit:embedding",
        {
            "config_hash": embedding_config_hash(),
            "error": error[:1000],
            "opened_at": datetime.now(UTC).isoformat(),
            "permanent": permanent,
            "state": "open",
        },
    )


def embedding_circuit_is_open(conn: sqlite3.Connection) -> bool:
    state = get_system_state(conn, "circuit:embedding")
    if not state or state.get("state") != "open":
        return False
    if state.get("config_hash") != embedding_config_hash():
        set_system_state(conn, "circuit:embedding", {"state": "closed", "reason": "configuration changed"})
        return False
    return bool(state.get("permanent"))


def reset_embedding_circuit(conn: sqlite3.Connection) -> None:
    set_system_state(conn, "circuit:embedding", {"state": "closed", "closed_at": datetime.now(UTC).isoformat()})


def is_permanent_provider_error(message: str) -> bool:
    normalized = message.casefold()
    markers = ("401", "403", "forbidden", "unauthorized", "invalid api key", "доступ запрещен")
    return any(marker in normalized for marker in markers)
