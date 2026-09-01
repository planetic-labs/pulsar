from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Path

from app.auth import require_access_token, require_admin
from app.database import Database
from app.dependencies import (
    get_database,
    get_deepgram,
    get_google_drive,
    get_manticore,
    get_settings,
)
from app.indexing_state import enqueue_index_task_async
from app.ports import FileStoragePort, TranscriptionPort, VectorStorePort
from app.settings import Settings
from app.worker import get_worker

logger = logging.getLogger("app.routers.worker")

router = APIRouter(tags=["Worker & Tasks"])


@router.post("/api/v1/worker/start")
async def api_worker_start(_: str = Depends(require_admin)) -> dict[str, str]:
    """Starts the background processing worker if not already running."""
    worker = get_worker()
    if not worker.is_running:
        worker.start()
        return {"status": "starting"}
    return {"status": "already_running"}


@router.post("/api/v1/worker/stop")
async def api_worker_stop(_: str = Depends(require_admin)) -> dict[str, str]:
    """Stops the background processing worker."""
    worker = get_worker()
    if worker.is_running:
        worker.stop()
        return {"status": "stopping"}
    return {"status": "already_stopped"}


@router.get("/api/v1/worker/status")
async def api_worker_status(_: str = Depends(require_access_token)) -> dict[str, Any]:
    """Returns the current state and status of the background worker."""
    worker = get_worker()
    worker_status: str = "stopped"
    if worker.is_stopping:
        worker_status = "stopping"
    elif worker.is_running:
        worker_status = "running"

    return {"status": worker_status, "is_running": worker.is_running}


@router.get("/api/v1/worker/progress")
async def api_worker_progress(
    db: Database = Depends(get_database),
    dg_adapter: TranscriptionPort = Depends(get_deepgram),
    _: str = Depends(require_access_token),
) -> dict[str, Any]:
    """Returns real-time progress for all stages, queue counts, and processing stats."""
    worker = get_worker()
    state = worker.get_progress_state()

    counts: dict[str, int] = {"stage_1_download": 0, "stage_2_transcribe": 0, "stage_3_index": 0}
    stats: dict[str, Any] = {"pending": 0, "failed": 0, "recent_errors": []}

    async with db.transaction() as conn:
        # Get pending tasks per stage
        sql_q = "SELECT task_type, COUNT(*) as c FROM tasks WHERE status = 'pending' GROUP BY task_type"
        async with conn.execute(sql_q) as cursor:
            rows = await cursor.fetchall()
        for r in rows:
            ttype: str = r["task_type"]
            if ttype == "ingest_video":
                ttype = "stage_1_download"
            if ttype in counts:
                counts[ttype] += r["c"]

        # Get aggregate stats
        sql_s = "SELECT status, COUNT(*) as c FROM tasks GROUP BY status"
        async with conn.execute(sql_s) as cursor:
            s_rows = await cursor.fetchall()
        stats["skipped_silent_list"] = []
        stats["skipped_no_space_list"] = []
        for r in s_rows:
            if r["status"] == "pending":
                stats["pending"] = r["c"]
            if r["status"] == "failed":
                stats["failed"] = r["c"]
            if r["status"] == "skipped_no_space":
                stats["skipped_no_space"] = r["c"]

        # Detail skipped no space
        skipped_no_space_count = stats.get("skipped_no_space", 0)
        if isinstance(skipped_no_space_count, int) and skipped_no_space_count > 0:
            sql_ns = "SELECT id, payload FROM tasks WHERE status = 'skipped_no_space' ORDER BY updated_at DESC LIMIT 20"
            async with conn.execute(sql_ns) as cursor:
                ns_rows = await cursor.fetchall()
            for nsr in ns_rows:
                try:
                    p = json.loads(nsr["payload"])
                    size_gb: float = p.get("file_size", 0) / (1024**3)
                    stats["skipped_no_space_list"].append(
                        {"id": nsr["id"], "title": p.get("title") or "Unknown", "size": f"{size_gb:.2f} ГБ"}
                    )
                except Exception:
                    pass

        # Empty structures kept for backward compatibility
        stats["missing_on_drive_list"] = []
        stats["missing_on_drive_count"] = 0
        stats["excluded_by_keyword_list"] = []
        stats["excluded_by_keyword_count"] = 0
        stats["missing_transcripts_list"] = []
        stats["missing_transcripts_count"] = 0
        stats["integrity_issues"] = []

        try:
            sql_ii = "SELECT id, message FROM integrity_issues ORDER BY id DESC LIMIT 100"
            async with conn.execute(sql_ii) as cursor:
                ii_rows = await cursor.fetchall()

            for row in ii_rows:
                msg: str = row["message"]
                id_match = (
                    re.search(r"\(ID:(\d+)\)", msg)
                    or re.search(r"Video (\d+):", msg)
                    or re.search(r"video ID (\d+):", msg, re.IGNORECASE)
                )
                video_id: int | None = int(id_match.group(1)) if id_match else None

                source_url: str | None = None
                if video_id:
                    async with conn.execute("SELECT source_url FROM videos WHERE id = ?", (video_id,)) as cursor:
                        v_row = await cursor.fetchone()
                    if v_row:
                        source_url = v_row["source_url"]

                stats["integrity_issues"].append({"message": msg, "video_id": video_id, "source_url": source_url})
        except Exception as e:
            logger.error(f"Failed to fetch integrity issues: {e}")

        # Detail duplicate MD5 files (removed from UI, kept empty lists for API compatibility)
        stats["duplicate_md5_list"] = []
        stats["duplicate_md5_count"] = 0

        # Detail failed
        if stats["failed"] > 0:
            sql_e = """
                SELECT id, task_type, error_message, payload, retries, max_retries
                FROM tasks WHERE status = 'failed'
                ORDER BY updated_at DESC LIMIT 5
            """
            async with conn.execute(sql_e) as cursor:
                e_rows = await cursor.fetchall()
            for er in e_rows:
                title = "Unknown Task"
                try:
                    p = json.loads(er["payload"])
                    title = p.get("title") or p.get("file_id") or "Task"
                except Exception:
                    pass

                stats["recent_errors"].append(
                    {
                        "id": er["id"],
                        "title": title,
                        "type": er["task_type"],
                        "error": er["error_message"],
                        "retries": er["retries"] or 0,
                        "max_retries": er["max_retries"] or 3,
                    }
                )

    # Get balance from cache / provider
    balance_data = {}
    if hasattr(dg_adapter, "get_balance_async"):
        balance_data = await dg_adapter.get_balance_async(force_refresh=False)

    return {
        "stages": state,
        "queue_counts": counts,
        "stats": stats,
        "is_running": worker.is_running,
        "dg_balance": balance_data,
    }


@router.post("/api/v1/tasks/{task_id}/restart")
async def api_restart_task(
    task_id: int = Path(..., ge=1),
    db: Database = Depends(get_database),
    _: str = Depends(require_admin),
) -> dict[str, str]:
    """Restarts a failed or skipped background job/task."""
    async with db.transaction() as conn:
        await conn.execute("UPDATE tasks SET status = 'pending', error_message = NULL WHERE id = ?", (task_id,))

    # Auto-start worker
    worker = get_worker()
    worker.start()

    return {"status": "restarted"}


@router.get("/api/v1/deepgram/balance")
async def api_deepgram_balance(
    dg_adapter: TranscriptionPort = Depends(get_deepgram),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Returns Deepgram subscription balance and project limits."""
    if hasattr(dg_adapter, "get_balance_async"):
        return await dg_adapter.get_balance_async(force_refresh=True)
    return {"balances": []}


@router.post("/api/tasks/ingest")
async def api_add_ingest_task(
    file_id: str = Form(..., min_length=25, max_length=50, pattern="^[a-zA-Z0-9_-]+$"),
    title: str | None = Form(None, min_length=1, max_length=200),
    diarize: bool = Form(True),
    db: Database = Depends(get_database),
    drive_client: FileStoragePort = Depends(get_google_drive),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Creates a new download-transcribe-index sequence task from a Google Drive file."""
    parent_folder_id: str | None = None
    try:
        file_meta = await drive_client.get_file_metadata(file_id)
        md5: str | None = file_meta.get("md5_checksum")
        parents = file_meta.get("parents")
        parent_folder_id = parents[0] if parents else None
        file_name = file_meta.get("name")
    except Exception as e:
        logger.error(f"Failed to get file metadata for {file_id}: {e}")
        md5 = None
        file_name = f"File {file_id}"

    async with db.transaction() as conn:
        # Check if this file_id is already registered
        async with conn.execute("SELECT id FROM videos WHERE source_file_id = ?", (file_id,)) as cursor:
            existing_by_id = await cursor.fetchone()
        if existing_by_id:
            return {"status": "already_indexed", "video_id": existing_by_id["id"]}

        if md5:
            # Check for content duplicates (original video with this MD5)
            async with conn.execute(
                "SELECT id FROM videos WHERE md5_checksum = ? AND original_id IS NULL", (md5,)
            ) as cursor:
                existing_orig = await cursor.fetchone()
            if existing_orig:
                # Automate: insert as duplicate directly
                await conn.execute(
                    """
                    INSERT INTO videos (
                        source_file_id, parent_folder_id, md5_checksum, title,
                        status, original_id, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_file_id) DO UPDATE SET
                        parent_folder_id = EXCLUDED.parent_folder_id,
                        md5_checksum = EXCLUDED.md5_checksum,
                        title = EXCLUDED.title,
                        status = EXCLUDED.status,
                        original_id = EXCLUDED.original_id,
                        source_url = EXCLUDED.source_url,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        file_id,
                        parent_folder_id,
                        md5,
                        title or file_name,
                        "skipped_duplicate_md5",
                        existing_orig["id"],
                        f"https://drive.google.com/file/d/{file_id}/view",
                    ),
                )
                return {"status": "skipped_duplicate_md5", "video_id": existing_orig["id"]}

        await conn.execute(
            "INSERT INTO tasks (task_type, payload) VALUES (?, ?)",
            (
                "stage_1_download",
                json.dumps(
                    {
                        "file_id": file_id,
                        "title": title or file_name,
                        "diarize": diarize,
                        "md5": md5,
                        "parent_folder_id": parent_folder_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    # Auto-start worker
    worker = get_worker()
    worker.start()

    return {"status": "queued", "file_id": file_id}


@router.post("/api/v1/worker/duplicates/swap")
async def api_worker_duplicates_swap(
    original_id: int = Form(..., ge=1),
    duplicate_id: int = Form(..., ge=1),
    db: Database = Depends(get_database),
    manticore: VectorStorePort = Depends(get_manticore),
    settings: Settings = Depends(get_settings),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Swaps the roles of an original video and a duplicate video."""
    async with db.transaction() as conn:
        async with conn.execute(
            "SELECT id, md5_checksum, size_bytes, duration_sec, status AS processing_status, "
            "       source_file_id, title FROM videos WHERE id = ?",
            (original_id,),
        ) as cursor:
            orig_row = await cursor.fetchone()

        async with conn.execute(
            "SELECT id, md5_checksum, source_file_id, title FROM videos WHERE id = ?",
            (duplicate_id,),
        ) as cursor:
            dup_row = await cursor.fetchone()

        if not orig_row or not dup_row:
            raise HTTPException(status_code=404, detail="Файлы не найдены")

        # Verify MD5 match
        if orig_row["md5_checksum"] != dup_row["md5_checksum"]:
            raise HTTPException(status_code=400, detail="Файлы имеют разные контрольные суммы MD5")

        # Rename transcript files on disk
        orig_source = orig_row["source_file_id"]
        dup_source = dup_row["source_file_id"]
        if orig_source and dup_source:
            old_raw = settings.get_raw_transcript_path(orig_source)
            new_raw = settings.get_raw_transcript_path(dup_source)
            old_norm = settings.get_normalized_transcript_path(orig_source)
            new_norm = settings.get_normalized_transcript_path(dup_source)

            def rename_files() -> None:
                if old_raw.exists():
                    new_raw.parent.mkdir(parents=True, exist_ok=True)
                    old_raw.rename(new_raw)
                    if old_raw.parent.exists() and not any(old_raw.parent.iterdir()):
                        old_raw.parent.rmdir()
                if old_norm.exists():
                    new_norm.parent.mkdir(parents=True, exist_ok=True)
                    old_norm.rename(new_norm)
                    if old_norm.parent.exists() and not any(old_norm.parent.iterdir()):
                        old_norm.parent.rmdir()

            try:
                await asyncio.to_thread(rename_files)
            except Exception as e:
                logger.error(f"Failed to rename transcript files during swap: {e}")

        # Swap roles in SQLite
        # 1. Move chunks from original_id to duplicate_id
        await conn.execute("UPDATE chunks SET video_id = ? WHERE video_id = ?", (duplicate_id, original_id))

        # 2. Temporarily set md5_checksum of original_id to NULL
        # to avoid UNIQUE constraint violation on original_id IS NULL
        await conn.execute("UPDATE videos SET md5_checksum = NULL WHERE id = ?", (original_id,))

        # 3. Make duplicate_id the new original
        await conn.execute(
            "UPDATE videos "
            "SET original_id = NULL, status = 'transcribed', "
            "    size_bytes = ?, duration_sec = ? "
            "WHERE id = ?",
            (
                orig_row["size_bytes"],
                orig_row["duration_sec"],
                duplicate_id,
            ),
        )

        # 4. Make original_id the new duplicate and restore its md5_checksum
        await conn.execute(
            "UPDATE videos "
            "SET original_id = ?, status = 'skipped_duplicate_md5', "
            "    size_bytes = NULL, duration_sec = NULL, "
            "    md5_checksum = ? "
            "WHERE id = ?",
            (duplicate_id, orig_row["md5_checksum"], original_id),
        )

        # Update other duplicates that pointed to original_id to now point to duplicate_id
        await conn.execute(
            "UPDATE videos SET original_id = ? WHERE original_id = ?",
            (duplicate_id, original_id),
        )

        # 4. Queue a re-indexing task for the new original to rebuild vectors in Manticore with new metadata
        await enqueue_index_task_async(conn, video_id=duplicate_id, title=dup_row["title"], priority=5)

    # 5. Delete old points of original_id from Manticore
    try:
        await manticore.delete_points_by_where(settings.manticore_table, f"video_id = {original_id}")
    except Exception as e:
        logger.error(f"Failed to delete old points from Manticore during swap: {e}")

    # Auto-start worker to process stage_3_index for the new original
    worker = get_worker()
    worker.start()

    return {
        "status": "success",
        "message": "Роли успешно изменены. Новый оригинал поставлен в очередь на переиндексацию.",
    }


@router.post("/api/v1/worker/duplicates/save")
async def api_worker_duplicates_save(
    duplicate_id: int = Form(..., ge=1),
    db: Database = Depends(get_database),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Marks a duplicate video as saved/acknowledged."""
    async with db.transaction() as conn:
        async with conn.execute("SELECT id FROM videos WHERE id = ?", (duplicate_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Файл не найден")
    return {"status": "success", "message": "Роль дубликата сохранена"}


@router.get("/api/v1/tasks/active")
async def api_get_active_tasks(
    db: Database = Depends(get_database),
    _: str = Depends(require_access_token),
) -> list[dict[str, Any]]:
    """Returns currently running tasks for UI progress updates."""
    async with db.transaction() as conn:
        sql_running = "SELECT * FROM tasks WHERE status = 'running' ORDER BY created_at ASC"
        async with conn.execute(sql_running) as cursor:
            rows = await cursor.fetchall()

        processed: list[dict[str, Any]] = []
        for row in rows:
            t = dict(row)
            try:
                payload = json.loads(t["payload"])
                t["file_id"] = payload.get("file_id")
                if t["file_id"]:
                    sql_v = "SELECT title FROM videos WHERE source_file_id = ?"
                    async with conn.execute(sql_v, (t["file_id"],)) as v_cursor:
                        v = await v_cursor.fetchone()
                    t["title"] = v["title"] if v else f"Файл {t['file_id'][:8]}..."
                else:
                    t["title"] = payload.get("title", "AI Indexing")
            except Exception:
                t["title"] = "Task"
            processed.append(t)
        return processed


@router.delete("/api/v1/tasks/{task_id}")
async def api_delete_task(
    task_id: int = Path(..., ge=1),
    db: Database = Depends(get_database),
    _: str = Depends(require_admin),
) -> dict[str, str]:
    """Removes a task from the processing queue."""
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"status": "deleted"}


@router.post("/api/v1/tasks/restart_failed")
async def api_restart_failed_tasks(
    db: Database = Depends(get_database),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Requeues all tasks that have failed with errors."""
    async with db.transaction() as conn:
        res = await conn.execute("UPDATE tasks SET status = 'pending', error_message = NULL WHERE status = 'failed'")
        count: int = res.rowcount

    # Auto-start worker
    worker = get_worker()
    worker.start()

    return {"status": "restarted", "count": count}


@router.post("/api/v1/tasks/restart_no_space")
async def api_restart_no_space_tasks(
    db: Database = Depends(get_database),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Requeues tasks that failed due to temporary storage space constraints."""
    async with db.transaction() as conn:
        res = await conn.execute(
            "UPDATE tasks SET status = 'pending', error_message = NULL WHERE status = 'skipped_no_space'"
        )
        count: int = res.rowcount

    # Auto-start worker
    worker = get_worker()
    worker.start()

    return {"status": "restarted", "count": count}


@router.post("/api/v1/reindex/all")
async def api_reindex_all(
    clear_manticore: bool = False,
    db: Database = Depends(get_database),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Requeues all fully processed videos to rebuild vector index search points in Manticore."""
    if clear_manticore:
        raise HTTPException(
            status_code=409,
            detail="Destructive clear is disabled. Run scripts/reindex_search.py --full for staging validation.",
        )

    async with db.transaction() as conn:
        # Find all videos that have at least one chunk
        async with conn.execute("""
            SELECT DISTINCT video_id, title
            FROM chunks c
            JOIN videos v ON v.id = c.video_id
        """) as cursor:
            rows = await cursor.fetchall()

        count: int = 0
        for r in rows:
            vid = r["video_id"]
            title = r["title"]
            task_id = await enqueue_index_task_async(conn, video_id=vid, title=title, priority=10)
            count += int(task_id is not None)

    # Auto-start worker
    worker = get_worker()
    worker.start()

    return {"status": "queued", "count": count}


@router.post("/api/v1/reindex/integrity")
async def api_reindex_integrity(
    db: Database = Depends(get_database),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Queues all videos with detected integrity issues for reindexing."""
    async with db.transaction() as conn:
        async with conn.execute("SELECT id, message FROM integrity_issues") as cursor:
            ii_rows = await cursor.fetchall()

        video_ids = set()
        for row in ii_rows:
            msg: str = row["message"]
            id_match = (
                re.search(r"\(ID:(\d+)\)", msg)
                or re.search(r"Video (\d+):", msg)
                or re.search(r"video ID (\d+):", msg, re.IGNORECASE)
            )
            if id_match:
                video_ids.add(int(id_match.group(1)))

        if not video_ids:
            return {"status": "success", "count": 0, "message": "Нет видео с проблемами целостности."}

        count = 0
        for vid in video_ids:
            async with conn.execute("SELECT source_file_id, title FROM videos WHERE id = ?", (vid,)) as cursor:
                v_row = await cursor.fetchone()
            if not v_row:
                continue

            source_file_id = v_row["source_file_id"]
            title = v_row["title"]

            # Check if task already exists to avoid duplicates
            sql_check = """
                SELECT 1 FROM tasks
                WHERE status IN ('pending', 'running')
                  AND (
                    video_id = ?
                    OR json_extract(payload, '$.file_id') = ?
                    OR json_extract(payload, '$.video_id') = ?
                  )
            """
            async with conn.execute(sql_check, (vid, source_file_id, vid)) as cursor:
                exists = await cursor.fetchone()
            if exists:
                continue

            # Queue stage_1_download with reindex flag
            payload = {"file_id": source_file_id, "title": title, "diarize": True, "reindex": True, "video_id": vid}
            await conn.execute(
                "INSERT INTO tasks (task_type, payload, status, priority, video_id) VALUES (?, ?, ?, ?, ?)",
                ("stage_1_download", json.dumps(payload, ensure_ascii=False), "pending", 8, vid),
            )

            # Reset video status to pending
            await conn.execute(
                """
                UPDATE videos
                SET status = 'pending', duration_sec = NULL, size_bytes = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (vid,),
            )

            # Remove matching integrity issues
            for row in ii_rows:
                msg = row["message"]
                id_match = (
                    re.search(r"\(ID:(\d+)\)", msg)
                    or re.search(r"Video (\d+):", msg)
                    or re.search(r"video ID (\d+):", msg, re.IGNORECASE)
                )
                if id_match and int(id_match.group(1)) == vid:
                    await conn.execute("DELETE FROM integrity_issues WHERE id = ?", (row["id"],))

            count += 1

    # Auto-start worker
    if count > 0:
        worker = get_worker()
        worker.start()

    return {"status": "success", "count": count}
