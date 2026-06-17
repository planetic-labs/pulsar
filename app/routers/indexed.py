from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Response, status

from app.auth import require_access_token, require_admin
from app.database import Database
from app.dependencies import get_database, get_google_drive, get_video_service
from app.ports import FileStoragePort
from app.services.video import VideoNotFoundError, VideoProcessingError, VideoService

logger = logging.getLogger("app.routers.indexed")

router = APIRouter(tags=["Indexed & Folders"])


@router.post("/api/v1/indexed/videos/{video_id}/toggle_short")
async def api_toggle_short(
    video_id: int,
    video_service: VideoService = Depends(get_video_service),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Toggles is_short status of a video and re-queues it for indexing."""
    try:
        return await video_service.toggle_short(video_id)
    except VideoNotFoundError as e:
        raise HTTPException(status_code=404, detail="Video not found") from e


@router.get("/api/v1/indexed/videos/{video_id}")
async def api_indexed_video_details(
    video_id: int,
    video_service: VideoService = Depends(get_video_service),
    _: str = Depends(require_access_token),
) -> dict[str, Any]:
    """Returns detailed information about an indexed video."""
    try:
        return await video_service.get_video_details(video_id)
    except VideoNotFoundError as e:
        raise HTTPException(status_code=404, detail="Video not found") from e


@router.delete("/api/v1/indexed/videos/{video_id}")
async def api_indexed_delete_video(
    video_id: int,
    video_service: VideoService = Depends(get_video_service),
    _: str = Depends(require_admin),
) -> dict[str, str]:
    """Deletes a video, its local files, its chunks and vector index search points."""
    try:
        await video_service.delete_video(video_id)
        return {"status": "success"}
    except VideoNotFoundError as e:
        raise HTTPException(status_code=404, detail="Video not found") from e
    except VideoProcessingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/api/v1/indexed/videos/{video_id}/reindex")
async def api_reindex_video(
    video_id: int,
    video_service: VideoService = Depends(get_video_service),
    _: str = Depends(require_admin),
) -> dict[str, str]:
    """Triggers a clean reindexing of a video by queuing a download task."""
    try:
        await video_service.reindex_video(video_id)
        return {"status": "reindexed"}
    except VideoNotFoundError as e:
        raise HTTPException(status_code=404, detail="Video not found") from e
    except VideoProcessingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/api/v1/indexed/videos/{video_id}/mark_silent")
async def api_mark_video_silent(
    video_id: int,
    video_service: VideoService = Depends(get_video_service),
    _: str = Depends(require_admin),
) -> dict[str, str]:
    """Marks a video as silent, deletes its chunks from SQLite and Manticore, and cancels active tasks."""
    try:
        await video_service.mark_video_silent(video_id)
        return {"status": "success"}
    except VideoNotFoundError as e:
        raise HTTPException(status_code=404, detail="Video not found") from e


@router.get("/api/v1/indexed/ls")
async def api_indexed_ls(
    folder_id: str | None = None,
    db: Database = Depends(get_database),
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    """Lists indexed folders and videos from local DB with metadata."""
    target_id: str | None = folder_id if folder_id and folder_id != "root" else None

    async with db.transaction() as conn:
        # 1. Get subfolders
        if target_id:
            sql_f = "SELECT id, name FROM folders WHERE parent_id = ? ORDER BY name ASC"
            async with conn.execute(sql_f, (target_id,)) as cursor:
                f_rows = await cursor.fetchall()
        else:
            sql_f = """
                SELECT id, name FROM folders
                WHERE parent_id IS NULL OR parent_id NOT IN (SELECT id FROM folders)
                ORDER BY name ASC
                """
            async with conn.execute(sql_f) as cursor:
                f_rows = await cursor.fetchall()

        # 2. Get videos in this folder
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
            params: tuple[Any, ...] = (target_id,)
        else:
            where = "v.parent_folder_id IS NULL OR v.parent_folder_id NOT IN (SELECT id FROM folders)"
            params = ()

        async with conn.execute(video_sql.format(where_clause=where), params) as cursor:
            v_rows = await cursor.fetchall()

        # 3. Get current folder path (breadcrumbs)
        path: list[dict[str, str]] = []
        curr: str | None = target_id
        while curr:
            async with conn.execute("SELECT id, name, parent_id FROM folders WHERE id = ?", (curr,)) as cursor:
                row = await cursor.fetchone()
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
    name: str = Form(...),
    parent_id: str | None = Form(None),
    _: str = Depends(require_admin),
) -> Response:
    """Manual folder creation is disabled (synced automatically with Google Drive)."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное управление структурой папок отключено. Структура синхронизируется автоматически с Google Drive.",
    )


@router.post("/api/v1/indexed/move")
async def api_indexed_move(
    video_id: int = Form(...),
    folder_id: str | None = Form(None),
    _: str = Depends(require_admin),
) -> Response:
    """Manual file moving is disabled (synced automatically with Google Drive)."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное перемещение файлов отключено. Структура синхронизируется автоматически с Google Drive.",
    )


@router.post("/api/v1/indexed/folders/rename")
async def api_indexed_rename_folder(
    folder_id: str = Form(...),
    new_name: str = Form(...),
    _: str = Depends(require_admin),
) -> Response:
    """Manual folder renaming is disabled (synced automatically with Google Drive)."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Ручное переименование папок отключено. Структура синхронизируется автоматически с Google Drive.",
    )


@router.delete("/api/v1/indexed/folders/{folder_id}")
async def api_indexed_delete_folder(
    folder_id: str,
    _: str = Depends(require_admin),
) -> Response:
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
    folder_id: str | None = None,
    refresh: bool = False,
    db: Database = Depends(get_database),
    drive: FileStoragePort = Depends(get_google_drive),
    _: str = Depends(require_access_token),
) -> list[dict[str, Any]]:
    """Lists contents of a Google Drive folder and maps their indexed/queued statuses."""
    target_id = folder_id or "root"

    try:
        # Поскольку list_folder_contents не в Protocol, но есть в GoogleDriveAdapter
        if hasattr(drive, "list_folder_contents"):
            items = await drive.list_folder_contents(target_id, use_cache=not refresh)  # type: ignore
        else:
            items = []

        async with db.transaction() as conn:
            # 1. Загрузка видео для проверки индексации
            async with conn.execute("SELECT source_file_id, md5_checksum FROM videos") as cursor:
                indexed_rows = await cursor.fetchall()
            indexed_ids = {row["source_file_id"] for row in indexed_rows}
            indexed_md5s = {row["md5_checksum"] for row in indexed_rows if row["md5_checksum"]}

            # 2. Загрузка папок
            async with conn.execute("SELECT id FROM folders") as cursor:
                folder_rows = await cursor.fetchall()
            indexed_folder_ids = {row["id"] for row in folder_rows}

            # 3. Активные задачи
            sql_q = """
                SELECT json_extract(payload, '$.file_id') as file_id,
                       json_extract(payload, '$.video_id') as video_id
                FROM tasks WHERE status IN ('pending', 'running')
            """
            async with conn.execute(sql_q) as cursor:
                queued_rows = await cursor.fetchall()

            queued_ids = set()
            video_ids_in_queue = []
            for r in queued_rows:
                if r["file_id"]:
                    queued_ids.add(r["file_id"])
                if r["video_id"]:
                    video_ids_in_queue.append(r["video_id"])

            if video_ids_in_queue:
                placeholders = ",".join(["?"] * len(video_ids_in_queue))
                async with conn.execute(
                    f"SELECT source_file_id FROM videos WHERE id IN ({placeholders})",
                    video_ids_in_queue,
                ) as cursor:
                    src_ids = await cursor.fetchall()
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
