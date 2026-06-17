from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.auth import require_access_token, require_admin
from app.dependencies import (
    get_chunk_repo,
    get_google_drive,
    get_video_repo,
    get_video_service,
)
from app.ports import FileStoragePort
from app.repos.chunk_repo import ChunkRepository
from app.repos.video_repo import VideoRepository
from app.services.video import VideoNotFoundError, VideoService

logger = logging.getLogger("app.routers.videos")

router = APIRouter(tags=["Videos, Chunks & Speakers"])


@router.get("/api/videos/{video_id}/speakers")
def api_list_speakers(video_id: int, _: str = Depends(require_access_token)) -> list[dict[str, str]]:
    """Returns mock list of speakers detected in a video."""
    return [{"tag": "primary", "name": "Основной голос"}]


@router.post("/api/videos/{video_id}/speakers")
async def api_save_speaker(
    video_id: int, request: Request, _: str = Depends(require_access_token)
) -> dict[str, str | bool]:
    """Updates/saves speaker metadata (disabled mock)."""
    return {"status": "saved", "enrolled": False}


@router.get("/api/videos/{video_id}/export")
async def api_video_export(
    video_id: int,
    video_repo: VideoRepository = Depends(get_video_repo),
    chunk_repo: ChunkRepository = Depends(get_chunk_repo),
    _: str = Depends(require_access_token),
) -> Response:
    """Exports full transcript text of a video as a .txt attachment file."""
    chunks = await chunk_repo.get_by_video_id(video_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="Transcript not found")

    full_text = "\n\n".join(c["text"] for c in chunks)
    v_row = await video_repo.get_by_id(video_id)
    filename: str = f"{v_row['title']}.txt" if v_row else f"transcript_{video_id}.txt"

    return Response(
        content=full_text,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/api/videos/{video_id}/chunks")
async def api_video_chunks(
    video_id: int,
    chunk_repo: ChunkRepository = Depends(get_chunk_repo),
    _: str = Depends(require_access_token),
) -> list[dict[str, Any]]:
    """Returns raw time-stamped transcript chunks for a video."""
    rows = await chunk_repo.get_by_video_id(video_id)
    return [dict(row) for row in rows]


@router.get("/api/videos")
async def api_list_videos(
    video_repo: VideoRepository = Depends(get_video_repo),
    _: str = Depends(require_access_token),
) -> list[dict[str, Any]]:
    """Returns list of all indexed video IDs and titles."""
    rows = await video_repo.get_all()
    return [dict(row) for row in rows]


@router.post("/api/chunks/{chunk_id}")
async def api_update_chunk(
    chunk_id: int,
    request: Request,
    video_service: VideoService = Depends(get_video_service),
    _: str = Depends(require_access_token),
) -> dict[str, Any]:
    """Updates a single chunk text in SQLite database, JSON transcript, and Manticore search index."""
    data = await request.json()
    new_text: str | None = data.get("text")
    if new_text is None:
        raise HTTPException(status_code=400, detail="Missing text")

    try:
        return await video_service.update_chunk_text(chunk_id, new_text)
    except VideoNotFoundError as e:
        raise HTTPException(status_code=404, detail="Chunk not found") from e


@router.post("/api/speakers/register")
async def api_register_speaker(
    video_id: int = Form(...),
    start_sec: float = Form(...),
    end_sec: float = Form(...),
    name: str = Form(...),
    _: str = Depends(require_admin),
) -> dict[str, str]:
    """Registers a manual speaker voice profile (disabled mock)."""
    logger.info(f"Manual speaker registration for '{name}' received but ignored (storage disabled).")
    return {"status": "success", "info": "Global enrollment disabled for now."}


@router.delete("/api/speakers/{speaker_id}")
async def api_delete_speaker(
    speaker_id: str,
    _: str = Depends(require_admin),
) -> dict[str, str]:
    """Deletes registered speaker database references (disabled mock)."""
    return {"status": "deleted"}


@router.get("/videos/{video_id}/file")
async def video_file(
    video_id: int,
    request: Request,
    video_repo: VideoRepository = Depends(get_video_repo),
    drive_client: FileStoragePort = Depends(get_google_drive),
) -> Response:
    require_access_token(request)

    row = await video_repo.get_by_id(video_id)
    if not row:
        raise HTTPException(status_code=404)

    if row["source_file_id"]:
        # Open the stream from Google Drive
        resp = await drive_client.open_media_stream(row["source_file_id"], range_header=request.headers.get("range"))

        # Prepare headers for the browser
        headers = {
            "Accept-Ranges": "bytes",
        }
        if resp.headers.get("Content-Range"):
            headers["Content-Range"] = str(resp.headers.get("Content-Range"))

        async def stream_from_resp(r):
            try:
                # Use smaller chunks for better reactivity
                async for chunk in r.aiter_bytes(chunk_size=256 * 1024):
                    yield chunk
            except Exception as e:
                logger.error(f"Streaming error for video {video_id}: {e}")
            finally:
                await r.aclose()
                # Close the associated client
                if hasattr(r, "_client"):
                    await r._client.aclose()

        return StreamingResponse(
            stream_from_resp(resp),
            status_code=resp.status_code,
            media_type=row["mime_type"] or "video/mp4",
            headers=headers,
        )

    raise HTTPException(status_code=404, detail="Local video streaming is not supported")
