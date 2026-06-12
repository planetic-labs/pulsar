from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException

from app.auth import require_access_token, require_admin
from app.config import (
    get_app_settings,
    get_deepgram_settings,
    get_google_drive_settings,
    get_manticore_settings,
    get_sqlite_settings,
)
from app.db import db_connection
from app.google_drive import GoogleDriveClient
from app.manticore import get_manticore_client
from app.worker import get_worker

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Worker & Tasks"])


@router.post("/api/v1/worker/start")
async def api_worker_start(_: str = Depends(require_admin)) -> dict[str, str]:
    """Starts the background processing worker if not already running."""
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())
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
async def api_worker_progress(_: str = Depends(require_access_token)) -> dict[str, Any]:
    """Returns real-time progress for all stages, queue counts, and processing stats."""
    worker = get_worker()
    state = worker.get_progress_state()

    settings = get_sqlite_settings()
    counts: dict[str, int] = {"stage_1_download": 0, "stage_2_transcribe": 0, "stage_3_index": 0}
    stats: dict[str, Any] = {"pending": 0, "failed": 0, "recent_errors": []}

    with db_connection(settings) as conn:
        # Get pending tasks per stage
        sql_q = "SELECT task_type, COUNT(*) as c FROM tasks WHERE status = 'pending' GROUP BY task_type"
        rows = conn.execute(sql_q).fetchall()
        for r in rows:
            ttype: str = r["task_type"]
            if ttype == "ingest_video":
                ttype = "stage_1_download"
            if ttype in counts:
                counts[ttype] += r["c"]

        # Get aggregate stats
        sql_s = "SELECT status, COUNT(*) as c FROM tasks GROUP BY status"
        s_rows = conn.execute(sql_s).fetchall()
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
            ns_rows = conn.execute(sql_ns).fetchall()
            for nsr in ns_rows:
                try:
                    p = json.loads(nsr["payload"])
                    size_gb: float = p.get("file_size", 0) / (1024**3)
                    stats["skipped_no_space_list"].append(
                        {"id": nsr["id"], "title": p.get("title") or "Unknown", "size": f"{size_gb:.2f} ГБ"}
                    )
                except Exception:
                    pass

        # Detail missing on Google Drive
        stats["missing_on_drive_list"] = []
        stats["missing_on_drive_count"] = 0
        sql_mc = "SELECT COUNT(*) as c FROM videos WHERE is_missing = 1"
        stats["missing_on_drive_count"] = conn.execute(sql_mc).fetchone()["c"]
        if stats["missing_on_drive_count"] > 0:
            sql_m = (
                "SELECT id, title, source_file_id, source_url "
                "FROM videos WHERE is_missing = 1 "
                "ORDER BY updated_at DESC LIMIT 20"
            )
            m_rows = conn.execute(sql_m).fetchall()
            for mr in m_rows:
                stats["missing_on_drive_list"].append(
                    {
                        "id": mr["id"],
                        "title": mr["title"],
                        "file_id": mr["source_file_id"],
                        "source_url": mr["source_url"],
                    }
                )

        # Detail excluded by keyword
        stats["excluded_by_keyword_list"] = []
        stats["excluded_by_keyword_count"] = 0
        sql_ec = "SELECT COUNT(*) as c FROM videos WHERE is_excluded = 1"
        stats["excluded_by_keyword_count"] = conn.execute(sql_ec).fetchone()["c"]
        if stats["excluded_by_keyword_count"] > 0:
            sql_ex = (
                "SELECT id, title, source_file_id, source_url "
                "FROM videos WHERE is_excluded = 1 "
                "ORDER BY updated_at DESC LIMIT 20"
            )
            ex_rows = conn.execute(sql_ex).fetchall()
            for exr in ex_rows:
                stats["excluded_by_keyword_list"].append(
                    {
                        "id": exr["id"],
                        "title": exr["title"],
                        "file_id": exr["source_file_id"],
                        "source_url": exr["source_url"],
                    }
                )

        # Detail missing transcripts
        stats["missing_transcripts_list"] = []
        stats["missing_transcripts_count"] = 0

        sql_orig_videos = (
            "SELECT id, title, source_file_id, source_url, status "
            "FROM videos WHERE original_id IS NULL AND is_silent = 0 AND "
            "status NOT IN ('pending', 'failed', 'skipped_silent', 'skipped_no_space')"
        )
        orig_videos = conn.execute(sql_orig_videos).fetchall()

        app_settings = get_app_settings()
        for v in orig_videos:
            file_id = v["source_file_id"]
            if not file_id:
                continue
            raw_path = app_settings.get_raw_transcript_path(file_id)
            norm_path = app_settings.get_normalized_transcript_path(file_id)

            raw_missing: bool = not raw_path.exists()
            norm_missing: bool = not norm_path.exists()

            if raw_missing or norm_missing:
                reasons: list[str] = []
                if raw_missing:
                    reasons.append("нет RAW")
                if norm_missing:
                    reasons.append("нет NORMALIZED")
                reason_str: str = ", ".join(reasons)

                stats["missing_transcripts_list"].append(
                    {
                        "id": v["id"],
                        "title": v["title"],
                        "file_id": file_id,
                        "source_url": v["source_url"] or f"https://drive.google.com/file/d/{file_id}/view",
                        "reason": reason_str,
                    }
                )

        stats["missing_transcripts_count"] = len(stats["missing_transcripts_list"])

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
            e_rows = conn.execute(sql_e).fetchall()
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

        # Fetch integrity check issues
        stats["integrity_issues"] = []
        try:
            sql_ii = "SELECT message FROM integrity_issues ORDER BY id ASC"
            ii_rows = conn.execute(sql_ii).fetchall()

            import re

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
                    v_row = conn.execute("SELECT source_url FROM videos WHERE id = ?", (video_id,)).fetchone()
                    if v_row:
                        source_url = v_row["source_url"]

                stats["integrity_issues"].append({"message": msg, "video_id": video_id, "source_url": source_url})
        except Exception as e:
            logger.error(f"Failed to fetch integrity issues: {e}")

    # Get balance from cache
    from app.transcription.deepgram import DeepgramEngine

    dg_engine = DeepgramEngine(get_deepgram_settings())
    balance_data = dg_engine.get_balance()

    return {
        "stages": state,
        "queue_counts": counts,
        "stats": stats,
        "is_running": worker.is_running,
        "dg_balance": balance_data,
    }


@router.post("/api/v1/tasks/{task_id}/restart")
async def api_restart_task(task_id: int, _: str = Depends(require_admin)) -> dict[str, str]:
    """Restarts a failed or skipped background job/task."""
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        conn.execute("UPDATE tasks SET status = 'pending', error_message = NULL WHERE id = ?", (task_id,))

    # Auto-start worker
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "restarted"}


@router.get("/api/v1/deepgram/balance")
async def api_deepgram_balance(_: str = Depends(require_admin)) -> dict[str, Any]:
    """Returns Deepgram subscription balance and project limits."""
    from app.transcription.deepgram import DeepgramEngine

    settings = get_deepgram_settings()
    engine = DeepgramEngine(settings)
    return engine.get_balance()


@router.post("/api/tasks/ingest")
async def api_add_ingest_task(
    file_id: str = Form(...),
    title: str | None = Form(None),
    diarize: bool = Form(True),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Creates a new download-transcribe-index sequence task from a Google Drive file."""
    drive_client = GoogleDriveClient(get_google_drive_settings())
    parent_folder_id: str | None = None
    try:
        file_meta = await drive_client.get_file(file_id)
        md5: str | None = file_meta.md5_checksum
        parent_folder_id = file_meta.parents[0] if file_meta.parents else None
    except Exception as e:
        logger.error(f"Failed to get file metadata for {file_id}: {e}")
        md5 = None

    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        # Check if this file_id is already registered
        existing_by_id = conn.execute("SELECT id FROM videos WHERE source_file_id = ?", (file_id,)).fetchone()
        if existing_by_id:
            return {"status": "already_indexed", "video_id": existing_by_id["id"]}

        if md5:
            # Check for content duplicates (original video with this MD5)
            existing_orig = conn.execute(
                "SELECT id FROM videos WHERE md5_checksum = ? AND original_id IS NULL", (md5,)
            ).fetchone()
            if existing_orig:
                # Automate: insert as duplicate directly
                conn.execute(
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
                        title or (file_meta.name if "file_meta" in locals() else f"File {file_id}"),
                        "skipped_duplicate_md5",
                        existing_orig["id"],
                        f"https://drive.google.com/file/d/{file_id}/view",
                    ),
                )
                return {"status": "skipped_duplicate_md5", "video_id": existing_orig["id"]}

        conn.execute(
            "INSERT INTO tasks (task_type, payload) VALUES (?, ?)",
            (
                "stage_1_download",
                json.dumps(
                    {
                        "file_id": file_id,
                        "title": title or (file_meta.name if "file_meta" in locals() else f"File {file_id}"),
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
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "queued", "file_id": file_id}


@router.post("/api/v1/worker/duplicates/swap")
async def api_worker_duplicates_swap(
    original_id: int = Form(...),
    duplicate_id: int = Form(...),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Swaps the roles of an original video and a duplicate video."""
    pg_settings = get_sqlite_settings()
    q_settings = get_manticore_settings()

    with db_connection(pg_settings) as conn:
        orig_row = conn.execute(
            "SELECT id, md5_checksum, size_bytes, duration_sec, status AS processing_status, "
            "       source_file_id, title FROM videos WHERE id = ?",
            (original_id,),
        ).fetchone()

        dup_row = conn.execute(
            "SELECT id, md5_checksum, source_file_id, title FROM videos WHERE id = ?",
            (duplicate_id,),
        ).fetchone()

        if not orig_row or not dup_row:
            raise HTTPException(status_code=404, detail="Файлы не найдены")

        # Verify MD5 match
        if orig_row["md5_checksum"] != dup_row["md5_checksum"]:
            raise HTTPException(status_code=400, detail="Файлы имеют разные контрольные суммы MD5")

        # Rename transcript files on disk
        orig_source = orig_row["source_file_id"]
        dup_source = dup_row["source_file_id"]
        if orig_source and dup_source:
            app_settings = get_app_settings()

            # For raw:
            old_raw = app_settings.get_raw_transcript_path(orig_source)
            new_raw = app_settings.get_raw_transcript_path(dup_source)
            if old_raw.exists():
                try:
                    new_raw.parent.mkdir(parents=True, exist_ok=True)
                    old_raw.rename(new_raw)
                    if old_raw.parent.exists() and not any(old_raw.parent.iterdir()):
                        old_raw.parent.rmdir()
                except Exception as e:
                    logger.error(f"Failed to rename raw transcript {old_raw} to {new_raw}: {e}")

            # For normalized:
            old_norm = app_settings.get_normalized_transcript_path(orig_source)
            new_norm = app_settings.get_normalized_transcript_path(dup_source)
            if old_norm.exists():
                try:
                    new_norm.parent.mkdir(parents=True, exist_ok=True)
                    old_norm.rename(new_norm)
                    if old_norm.parent.exists() and not any(old_norm.parent.iterdir()):
                        old_norm.parent.rmdir()
                except Exception as e:
                    logger.error(f"Failed to rename normalized transcript {old_norm} to {new_norm}: {e}")

        # Swap roles in SQLite
        # 1. Move chunks from original_id to duplicate_id
        conn.execute("UPDATE chunks SET video_id = ? WHERE video_id = ?", (duplicate_id, original_id))

        # 2. Temporarily set md5_checksum of original_id to NULL
        # to avoid UNIQUE constraint violation on original_id IS NULL
        conn.execute("UPDATE videos SET md5_checksum = NULL WHERE id = ?", (original_id,))

        # 3. Make duplicate_id the new original
        conn.execute(
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
        conn.execute(
            "UPDATE videos "
            "SET original_id = ?, status = 'skipped_duplicate_md5', "
            "    size_bytes = NULL, duration_sec = NULL, "
            "    md5_checksum = ? "
            "WHERE id = ?",
            (duplicate_id, orig_row["md5_checksum"], original_id),
        )

        # Update other duplicates that pointed to original_id to now point to duplicate_id
        conn.execute(
            "UPDATE videos SET original_id = ? WHERE original_id = ?",
            (duplicate_id, original_id),
        )

        # 4. Queue a re-indexing task for the new original to rebuild vectors in Manticore with new metadata
        new_payload = {"video_id": duplicate_id, "title": dup_row["title"]}
        conn.execute(
            "INSERT INTO tasks (video_id, task_type, payload, status, priority) "
            "VALUES (?, 'stage_3_index', ?, 'pending', 5)",
            (duplicate_id, json.dumps(new_payload, ensure_ascii=False)),
        )

    # 5. Delete old points of original_id from Manticore
    try:
        manticore = get_manticore_client()
        manticore.delete(
            collection_name=q_settings.table_name,
            where_clause=f"video_id = {original_id}",
        )
    except Exception as e:
        logger.error(f"Failed to delete old points from Manticore during swap: {e}")

    # Auto-start worker to process stage_3_index for the new original
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {
        "status": "success",
        "message": "Роли успешно изменены. Новый оригинал поставлен в очередь на переиндексацию.",
    }


@router.post("/api/v1/worker/duplicates/save")
async def api_worker_duplicates_save(
    duplicate_id: int = Form(...),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Marks a duplicate video as saved/acknowledged."""
    pg_settings = get_sqlite_settings()
    with db_connection(pg_settings) as conn:
        row = conn.execute("SELECT id FROM videos WHERE id = ?", (duplicate_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Файл не найден")
    return {"status": "success", "message": "Роль дубликата сохранена"}


@router.get("/api/v1/tasks/active")
async def api_get_active_tasks(_: str = Depends(require_access_token)) -> list[dict[str, Any]]:
    """Returns currently running tasks for UI progress updates."""
    pg_settings = get_sqlite_settings()
    with db_connection(pg_settings) as connection:
        sql_running = "SELECT * FROM tasks WHERE status = 'running' ORDER BY created_at ASC"
        rows = connection.execute(sql_running).fetchall()

        processed: list[dict[str, Any]] = []
        for row in rows:
            t = dict(row)
            try:
                payload = json.loads(t["payload"])
                t["file_id"] = payload.get("file_id")
                if t["file_id"]:
                    sql_v = "SELECT title FROM videos WHERE source_file_id = ?"
                    v = connection.execute(sql_v, (t["file_id"],)).fetchone()
                    t["title"] = v["title"] if v else f"Файл {t['file_id'][:8]}..."
                else:
                    t["title"] = payload.get("title", "AI Indexing")
            except Exception:
                t["title"] = "Task"
            processed.append(t)
        return processed


@router.delete("/api/v1/tasks/{task_id}")
async def api_delete_task(task_id: int, _: str = Depends(require_access_token)) -> dict[str, str]:
    """Removes a task from the processing queue."""
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"status": "deleted"}


@router.post("/api/v1/tasks/restart_failed")
async def api_restart_failed_tasks(_: str = Depends(require_access_token)) -> dict[str, Any]:
    """Requeues all tasks that have failed with errors."""
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        res = conn.execute("UPDATE tasks SET status = 'pending', error_message = NULL WHERE status = 'failed'")
        count: int = res.rowcount

    # Auto-start worker
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "restarted", "count": count}


@router.post("/api/v1/tasks/restart_no_space")
async def api_restart_no_space_tasks(_: str = Depends(require_access_token)) -> dict[str, Any]:
    """Requeues tasks that failed due to temporary storage space constraints."""
    settings = get_sqlite_settings()
    with db_connection(settings) as conn:
        res = conn.execute(
            "UPDATE tasks SET status = 'pending', error_message = NULL WHERE status = 'skipped_no_space'"
        )
        count: int = res.rowcount

    # Auto-start worker
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "restarted", "count": count}


@router.post("/api/v1/reindex/all")
async def api_reindex_all(clear_manticore: bool = False, _: str = Depends(require_access_token)) -> dict[str, Any]:
    """Requeues all fully processed videos to rebuild vector index search points in Manticore."""
    settings = get_sqlite_settings()
    q_settings = get_manticore_settings()

    if clear_manticore:
        from app.manticore import init_manticore

        manticore = get_manticore_client()
        logger.info(f"Clearing Manticore table {q_settings.table_name} for full reindex")
        try:
            manticore.delete_collection(q_settings.table_name)
            init_manticore()
        except Exception as e:
            logger.error(f"Failed to clear Manticore: {e}")

    with db_connection(settings) as conn:
        # Find all videos that have at least one chunk
        rows = conn.execute("""
            SELECT DISTINCT video_id, title
            FROM chunks c
            JOIN videos v ON v.id = c.video_id
        """).fetchall()

        count: int = 0
        for r in rows:
            vid = r["video_id"]
            title = r["title"]
            payload = json.dumps({"video_id": vid, "title": title})

            # Check if task already exists to avoid duplicates
            sql_check = """
                SELECT 1 FROM tasks
                WHERE task_type = 'stage_3_index' AND status IN ('pending', 'running')
                AND video_id = ?
            """
            exists = conn.execute(sql_check, (vid,)).fetchone()

            if not exists:
                conn.execute(
                    "INSERT INTO tasks (video_id, task_type, payload, status, priority) VALUES (?, ?, ?, ?, ?)",
                    (vid, "stage_3_index", payload, "pending", 10),
                )
                count += 1

    # Auto-start worker
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "queued", "count": count}


@router.post("/api/v1/reindex/integrity")
async def api_reindex_integrity(_: str = Depends(require_access_token)) -> dict[str, Any]:
    """Queues all videos with detected integrity issues for reindexing."""
    settings = get_sqlite_settings()
    import re

    with db_connection(settings) as conn:
        ii_rows = conn.execute("SELECT id, message FROM integrity_issues").fetchall()

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
            v_row = conn.execute("SELECT source_file_id, title FROM videos WHERE id = ?", (vid,)).fetchone()
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
            exists = conn.execute(sql_check, (vid, source_file_id, vid)).fetchone()
            if exists:
                continue

            # Queue stage_1_download with reindex flag
            payload = {"file_id": source_file_id, "title": title, "diarize": True, "reindex": True, "video_id": vid}
            conn.execute(
                "INSERT INTO tasks (task_type, payload, status, priority, video_id) VALUES (?, ?, ?, ?, ?)",
                ("stage_1_download", json.dumps(payload, ensure_ascii=False), "pending", 8, vid),
            )

            # Reset video status to pending
            conn.execute(
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
                    conn.execute("DELETE FROM integrity_issues WHERE id = ?", (row["id"],))

            count += 1

    # Auto-start worker
    if count > 0:
        worker = get_worker()
        if not worker.is_running:
            asyncio.create_task(worker.run())

    return {"status": "success", "count": count}
