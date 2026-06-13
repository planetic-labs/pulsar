from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Response, status

from app.auth import require_access_token, require_admin
from app.config import (
    get_app_settings,
    get_google_drive_settings,
    get_manticore_settings,
    get_sqlite_settings,
)
from app.db import db_connection
from app.google_drive import GoogleDriveClient
from app.worker import get_worker

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Indexed & Folders"])


@router.post("/api/v1/indexed/videos/{video_id}/toggle_short")
async def api_toggle_short(video_id: int, _: str = Depends(require_admin)) -> dict[str, Any]:
    """Toggles is_short status of a video and re-queues it for indexing."""
    pg_settings = get_sqlite_settings()
    q_settings = get_manticore_settings()
    app_settings = get_app_settings()

    from app.chunking import chunk_from_utterances
    from app.manticore import get_manticore_client
    from app.repository import replace_chunks

    with db_connection(pg_settings) as conn:
        # 1. Toggle status
        row = conn.execute("SELECT is_short, title FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Video not found")

        new_is_short: bool = not bool(row["is_short"])
        conn.execute("UPDATE videos SET is_short = ? WHERE id = ?", (new_is_short, video_id))

        # 2. Get transcript to re-chunk
        v_row = conn.execute("SELECT source_file_id FROM videos WHERE id = ?", (video_id,)).fetchone()
        if v_row and v_row["source_file_id"]:
            norm_path = app_settings.get_normalized_transcript_path(v_row["source_file_id"])
            if norm_path.exists():
                import gzip

                with gzip.open(norm_path, "rt", encoding="utf-8") as f:
                    norm_payload = json.load(f)
                raw_chunks = norm_payload.get("utterances") or norm_payload.get("chunks") or []

                # 3. Re-chunk
                new_chunks = chunk_from_utterances(raw_chunks, single_chunk=new_is_short)

                # 4. Clear Manticore old points
                old_chunk_ids = [
                    r["id"] for r in conn.execute("SELECT id FROM chunks WHERE video_id = ?", (video_id,)).fetchall()
                ]
                if old_chunk_ids:
                    manticore = get_manticore_client()
                    manticore.delete(
                        collection_name=q_settings.table_name,
                        ids=old_chunk_ids,
                    )

                # 5. Clear SQLite chunks and save new ones
                replace_chunks(conn, video_id=video_id, chunks=new_chunks)

                # 6. Re-queue for Stage 3 indexing
                conn.execute(
                    "INSERT INTO tasks (video_id, task_type, payload) VALUES (?, ?, ?)",
                    (video_id, "stage_3_index", json.dumps({"video_id": video_id})),
                )

                # Auto-start worker if needed
                worker = get_worker()
                if not worker.is_running:
                    asyncio.create_task(worker.run())

        conn.commit()

    return {"status": "ok", "is_short": new_is_short, "queued": True}


@router.get("/api/v1/indexed/videos/{video_id}")
async def api_indexed_video_details(video_id: int, _: str = Depends(require_access_token)) -> dict[str, Any]:
    """Returns detailed information about an indexed video."""
    pg_settings = get_sqlite_settings()
    with db_connection(pg_settings) as conn:
        video = conn.execute(
            """
            SELECT v.*, f.name as folder_name, orig.title as original_title,
                   (SELECT COUNT(*) FROM chunks WHERE video_id = v.id) as chunk_count
            FROM videos v
            LEFT JOIN folders f ON v.parent_folder_id = f.id
            LEFT JOIN videos orig ON v.original_id = orig.id
            WHERE v.id = ?
            """,
            (video_id,),
        ).fetchone()

        if not video:
            raise HTTPException(status_code=404, detail="Video not found")

        return dict(video)


@router.delete("/api/v1/indexed/videos/{video_id}")
async def api_indexed_delete_video(video_id: int, _: str = Depends(require_admin)) -> dict[str, str]:
    """Deletes a video, its local files, its chunks and vector index search points."""
    pg_settings = get_sqlite_settings()
    q_settings = get_manticore_settings()

    from app.manticore import get_manticore_client

    with db_connection(pg_settings) as conn:
        video_row = conn.execute("SELECT source_file_id FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not video_row:
            raise HTTPException(status_code=404, detail="Video not found")

        source_file_id = video_row["source_file_id"]

        # Check for active running or pending tasks to avoid conflicts
        sql_check = """
            SELECT id FROM tasks
            WHERE status IN ('pending', 'running')
              AND (video_id = ? OR json_extract(payload, '$.file_id') = ? OR json_extract(payload, '$.video_id') = ?)
        """
        active_task = conn.execute(sql_check, (video_id, source_file_id, video_id)).fetchone()
        if active_task:
            raise HTTPException(status_code=400, detail="Нельзя удалить видео, которое сейчас обрабатывается воркером.")

        chunk_rows = conn.execute("SELECT id FROM chunks WHERE video_id = ?", (video_id,)).fetchall()

    # Delete points from Manticore if they exist
    chunk_ids = [c["id"] for c in chunk_rows]
    if chunk_ids:
        try:
            manticore = get_manticore_client()
            manticore.delete(
                collection_name=q_settings.table_name,
                ids=chunk_ids,
            )
        except Exception as e:
            logger.error(f"Failed to delete Manticore points for video {video_id}: {e}")

    # Delete files from the filesystem (with raw transcript archiving)
    app_settings = get_app_settings()
    archive_dir = app_settings.storage_dir / "transcripts" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    files_to_delete: list[Any] = []
    source_file_id: str | None = video_row["source_file_id"]
    if source_file_id:
        wav_p = app_settings.audio_dir / f"{source_file_id}.wav"
        ogg_p = app_settings.audio_dir / f"{source_file_id}.ogg"
        for p in [wav_p, ogg_p]:
            if p.exists():
                files_to_delete.append(p)

        # Check and clean downloads directory for any temporary video files
        downloads_dir = app_settings.downloads_dir
        if downloads_dir.exists():
            for p in downloads_dir.glob(f"{source_file_id}*"):
                files_to_delete.append(p)

        # Archive raw transcript if it exists
        raw_path = app_settings.get_raw_transcript_path(source_file_id)
        if raw_path.exists():
            try:
                import shutil

                archive_dest = archive_dir / f"video_{video_id}_{source_file_id}.json.gz"
                shutil.move(str(raw_path), str(archive_dest))
                logger.info(f"Moved raw transcript to {archive_dest}")
                # Remove empty prefix directory if empty
                if raw_path.parent.exists() and not any(raw_path.parent.iterdir()):
                    raw_path.parent.rmdir()
            except Exception as e:
                logger.error(f"Failed to move raw transcript {raw_path}: {e}")
                files_to_delete.append(raw_path)

        # Add normalized transcript to deletion list
        norm_path = app_settings.get_normalized_transcript_path(source_file_id)
        if norm_path.exists():
            files_to_delete.append(norm_path)

    for f_path in files_to_delete:
        try:
            if f_path.exists():
                f_path.unlink()
                if f_path.parent.exists() and f_path.parent not in (
                    app_settings.raw_transcripts_dir,
                    app_settings.normalized_transcripts_dir,
                ):
                    if not any(f_path.parent.iterdir()):
                        f_path.parent.rmdir()
        except Exception as e:
            logger.error(f"Failed to delete file {f_path}: {e}")

    # Actually delete the video row and its associated integrity issues
    with db_connection(pg_settings) as conn:
        conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))

        # Remove matching integrity issues
        import re

        ii_rows = conn.execute("SELECT id, message FROM integrity_issues").fetchall()
        for row in ii_rows:
            msg = row["message"]
            id_match = (
                re.search(r"\(ID:(\d+)\)", msg)
                or re.search(r"Video (\d+):", msg)
                or re.search(r"video ID (\d+):", msg, re.IGNORECASE)
            )
            if id_match and int(id_match.group(1)) == video_id:
                conn.execute("DELETE FROM integrity_issues WHERE id = ?", (row["id"],))

    return {"status": "success"}


@router.post("/api/v1/indexed/videos/{video_id}/reindex")
async def api_reindex_video(video_id: int, _: str = Depends(require_admin)) -> dict[str, str]:
    """Triggers a clean reindexing of a video by queuing a download task with reindex flag."""
    pg_settings = get_sqlite_settings()

    with db_connection(pg_settings) as conn:
        video_row = conn.execute("SELECT source_file_id, title FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not video_row:
            raise HTTPException(status_code=404, detail="Video not found")

        source_file_id = video_row["source_file_id"]
        title = video_row["title"]

        # Check for active running or pending tasks to avoid conflicts
        sql_check = """
            SELECT id FROM tasks
            WHERE status IN ('pending', 'running')
              AND (video_id = ? OR json_extract(payload, '$.file_id') = ? OR json_extract(payload, '$.video_id') = ?)
        """
        active_task = conn.execute(sql_check, (video_id, source_file_id, video_id)).fetchone()
        if active_task:
            raise HTTPException(
                status_code=400, detail="Видео уже находится в обработке или в очереди. Пожалуйста, подождите."
            )

        # Update SQLite: set status to pending, reset metadata
        conn.execute(
            "UPDATE videos SET status = 'pending', duration_sec = NULL, "
            "size_bytes = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (video_id,),
        )

        # Remove matching integrity issues
        import re

        ii_rows = conn.execute("SELECT id, message FROM integrity_issues").fetchall()
        for row in ii_rows:
            msg = row["message"]
            id_match = (
                re.search(r"\(ID:(\d+)\)", msg)
                or re.search(r"Video (\d+):", msg)
                or re.search(r"video ID (\d+):", msg, re.IGNORECASE)
            )
            if id_match and int(id_match.group(1)) == video_id:
                conn.execute("DELETE FROM integrity_issues WHERE id = ?", (row["id"],))

        # Insert stage_1_download task with reindex=True
        conn.execute(
            "INSERT INTO tasks (task_type, payload, status, priority, video_id) VALUES (?, ?, ?, ?, ?)",
            (
                "stage_1_download",
                json.dumps(
                    {"file_id": source_file_id, "title": title, "diarize": True, "reindex": True, "video_id": video_id},
                    ensure_ascii=False,
                ),
                "pending",
                8,
                video_id,
            ),
        )

    # Auto-start worker
    worker = get_worker()
    if not worker.is_running:
        asyncio.create_task(worker.run())

    return {"status": "reindexed"}


@router.post("/api/v1/indexed/videos/{video_id}/mark_silent")
async def api_mark_video_silent(video_id: int, _: str = Depends(require_admin)) -> dict[str, str]:
    """Marks a video as silent, deletes its chunks from SQLite and Manticore, and cancels active tasks."""
    pg_settings = get_sqlite_settings()
    q_settings = get_manticore_settings()

    from app.manticore import get_manticore_client

    with db_connection(pg_settings) as conn:
        video_row = conn.execute("SELECT source_file_id FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not video_row:
            raise HTTPException(status_code=404, detail="Video not found")

        source_file_id = video_row["source_file_id"]

        # 1. Get chunk IDs from SQLite
        chunk_rows = conn.execute("SELECT id FROM chunks WHERE video_id = ?", (video_id,)).fetchall()
        chunk_ids = [c["id"] for c in chunk_rows]

        # 2. Delete points from Manticore if they exist
        if chunk_ids:
            try:
                manticore = get_manticore_client()
                manticore.delete(
                    collection_name=q_settings.table_name,
                    ids=chunk_ids,
                )
            except Exception as e:
                logger.error(f"Failed to delete Manticore points for video {video_id}: {e}")

        # 3. Delete chunks from SQLite
        conn.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))

        # 4. Delete tasks for this video
        conn.execute(
            """
            DELETE FROM tasks
            WHERE video_id = ?
               OR json_extract(payload, '$.file_id') = ?
               OR json_extract(payload, '$.video_id') = ?
            """,
            (video_id, source_file_id, video_id),
        )

        # 5. Update SQLite: set is_silent = 1, status = 'indexed_chunks_ready'
        conn.execute(
            """
            UPDATE videos
            SET is_silent = 1, status = 'indexed_chunks_ready', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (video_id,),
        )

        # 6. Remove matching integrity issues
        import re

        ii_rows = conn.execute("SELECT id, message FROM integrity_issues").fetchall()
        for row in ii_rows:
            msg = row["message"]
            id_match = (
                re.search(r"\(ID:(\d+)\)", msg)
                or re.search(r"Video (\d+):", msg)
                or re.search(r"video ID (\d+):", msg, re.IGNORECASE)
            )
            if id_match and int(id_match.group(1)) == video_id:
                conn.execute("DELETE FROM integrity_issues WHERE id = ?", (row["id"],))

        conn.commit()

    return {"status": "success"}


@router.get("/api/v1/indexed/ls")
async def api_indexed_ls(folder_id: str | None = None, _: str = Depends(require_admin)) -> dict[str, Any]:
    """Lists indexed folders and videos from local DB with metadata."""
    pg_settings = get_sqlite_settings()
    target_id: str | None = folder_id if folder_id and folder_id != "root" else None

    with db_connection(pg_settings) as connection:
        # 1. Get subfolders
        if target_id:
            sql_f = "SELECT id, name FROM folders WHERE parent_id = ? ORDER BY name ASC"
            f_rows = connection.execute(sql_f, (target_id,)).fetchall()
        else:
            sql_f = """
                SELECT id, name FROM folders
                WHERE parent_id IS NULL OR parent_id NOT IN (SELECT id FROM folders)
                ORDER BY name ASC
                """
            f_rows = connection.execute(sql_f).fetchall()

        # 2. Get videos in this folder with rich metadata
        video_sql = """
            SELECT
                v.id, v.title, v.mime_type, v.size_bytes, v.duration_sec,
                v.updated_at, v.source_file_id, v.is_short, v.is_4k, v.is_silent,
                (v.original_id IS NOT NULL) AS is_md5_duplicate,
                (SELECT COUNT(*) FROM chunks c WHERE c.video_id = v.id) as chunk_count
            FROM videos v
            WHERE {where_clause}
            ORDER BY v.title ASC
        """

        if target_id:
            where = "v.parent_folder_id = ?"
            params = (target_id,)
        else:
            where = "v.parent_folder_id IS NULL OR v.parent_folder_id NOT IN (SELECT id FROM folders)"
            params = ()

        v_rows = connection.execute(video_sql.format(where_clause=where), params).fetchall()

        # 3. Get current folder path (breadcrumbs)
        path: list[dict[str, str]] = []
        curr: str | None = target_id
        while curr:
            row = connection.execute("SELECT id, name, parent_id FROM folders WHERE id = ?", (curr,)).fetchone()
            if row:
                path.append({"id": row["id"], "name": row["name"]})
                curr = str(row["parent_id"]) if row["parent_id"] else None
            else:
                break
        path.reverse()

    items: list[dict[str, Any]] = []
    for r in f_rows:
        items.append({"id": r["id"], "name": r["name"], "is_folder": True, "mime_type": "folder"})

    for r in v_rows:
        items.append(
            {
                "id": r["id"],
                "name": r["title"],
                "is_folder": False,
                "mime_type": r["mime_type"],
                "is_short": bool(r["is_short"]),
                "is_4k": bool(r["is_4k"]),
                "is_md5_duplicate": bool(r["is_md5_duplicate"]),
                "is_silent": bool(r["is_silent"]),
                "source_file_id": r["source_file_id"],
                "duration_sec": r["duration_sec"],
                "chunk_count": r["chunk_count"],
                "language": "ru",
                "confidence": 1.0,
                "updated_at": r["updated_at"],
            }
        )

    return {"items": items, "path": path}


@router.post("/api/v1/indexed/mkdir")
async def api_indexed_mkdir(
    name: str = Form(...), parent_id: str | None = Form(None), _: str = Depends(require_admin)
) -> Response:
    """Manual folder creation is disabled (synced automatically with Google Drive)."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное управление структурой папок отключено. Структура синхронизируется автоматически с Google Drive.",
    )


@router.post("/api/v1/indexed/move")
async def api_indexed_move(
    video_id: int = Form(...), folder_id: str | None = Form(None), _: str = Depends(require_admin)
) -> Response:
    """Manual file moving is disabled (synced automatically with Google Drive)."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное перемещение файлов отключено. Структура синхронизируется автоматически с Google Drive.",
    )


@router.post("/api/v1/indexed/folders/rename")
async def api_indexed_rename_folder(
    folder_id: str = Form(...), new_name: str = Form(...), _: str = Depends(require_admin)
) -> Response:
    """Manual folder renaming is disabled (synced automatically with Google Drive)."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное переименование папок отключено. Структура синхронизируется автоматически с Google Drive.",
    )


@router.delete("/api/v1/indexed/folders/{folder_id}")
async def api_indexed_delete_folder(folder_id: str, _: str = Depends(require_admin)) -> Response:
    """Manual folder deletion is disabled (synced automatically with Google Drive)."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное удаление папок отключено. Структура синхронизируется автоматически с Google Drive.",
    )


@router.post("/api/v1/indexed/sync")
async def api_indexed_sync(_: str = Depends(require_admin)) -> dict[str, Any]:
    """Trigger metadata synchronization for all indexed files."""
    from scripts.sync_titles import sync_indexed_metadata

    try:
        count = await sync_indexed_metadata()
        return {"status": "success", "updated_count": count}
    except Exception as e:
        logger.error(f"Metadata sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/drive/ls")
async def api_drive_ls(
    folder_id: str | None = None, refresh: bool = False, _: str = Depends(require_access_token)
) -> list[dict[str, Any]]:
    """Lists contents of a Google Drive folder and maps their indexed/queued statuses."""
    drive_client = GoogleDriveClient(get_google_drive_settings())
    target_id: str = folder_id or "root"

    try:
        items = await drive_client.list_folder_contents(target_id, use_cache=not refresh)

        sqlite_settings = get_sqlite_settings()
        with db_connection(sqlite_settings) as connection:
            indexed_rows = connection.execute("SELECT source_file_id, md5_checksum FROM videos").fetchall()
            indexed_ids = {row["source_file_id"] for row in indexed_rows}
            indexed_md5s = {row["md5_checksum"] for row in indexed_rows if row["md5_checksum"]}

            folder_rows = connection.execute("SELECT id FROM folders").fetchall()
            indexed_folder_ids = {row["id"] for row in folder_rows}

            sql_q = """
                SELECT json_extract(payload, '$.file_id') as file_id,
                       json_extract(payload, '$.video_id') as video_id
                FROM tasks WHERE status IN ('pending', 'running')
            """
            queued_rows = connection.execute(sql_q).fetchall()

            queued_ids = set()
            video_ids_in_queue = []
            for r in queued_rows:
                if r["file_id"]:
                    queued_ids.add(r["file_id"])
                if r["video_id"]:
                    video_ids_in_queue.append(r["video_id"])

            if video_ids_in_queue:
                placeholders = ",".join(["?"] * len(video_ids_in_queue))
                src_ids = connection.execute(
                    f"SELECT source_file_id FROM videos WHERE id IN ({placeholders})", video_ids_in_queue
                ).fetchall()
                for s in src_ids:
                    if s["source_file_id"]:
                        queued_ids.add(s["source_file_id"])

        for item in items:
            if item.get("is_folder"):
                item["is_indexed"] = item["id"] in indexed_folder_ids
            else:
                item["is_indexed"] = (item["id"] in indexed_ids) or (item.get("md5_checksum") in indexed_md5s)

            item["is_queued"] = item["id"] in queued_ids

        return items
    except Exception as e:
        if "token" in str(e).lower() or "auth" in str(e).lower():
            raise HTTPException(
                status_code=401, detail="Google Drive access error. Check service account permissions."
            ) from e
        raise HTTPException(status_code=500, detail=str(e)) from e
