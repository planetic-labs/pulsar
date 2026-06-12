from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.auth import require_access_token, require_admin
from app.config import (
    get_app_settings,
    get_embedding_settings,
    get_google_drive_settings,
    get_manticore_settings,
    get_sqlite_settings,
)
from app.db import db_connection
from app.embeddings import UnifiedEmbeddingClient
from app.google_drive import GoogleDriveClient
from app.manticore import get_manticore_client

logger = logging.getLogger(__name__)

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
def api_video_export(video_id: int, _: str = Depends(require_access_token)) -> Response:
    """Exports full transcript text of a video as a .txt attachment file."""
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT c.text
            FROM chunks c
            WHERE c.video_id = ?
            ORDER BY c.chunk_index ASC
            """,
            (video_id,),
        ).fetchall()

        if not rows:
            raise HTTPException(status_code=404, detail="Transcript not found")

        full_text_lines = []
        for r in rows:
            full_text_lines.append(r["text"])

        full_text = "\n\n".join(full_text_lines)
        v_row = connection.execute("SELECT title FROM videos WHERE id = ?", (video_id,)).fetchone()
        filename: str = f"{v_row['title']}.txt" if v_row else f"transcript_{video_id}.txt"

        return Response(
            content=full_text,
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )


@router.get("/api/videos/{video_id}/chunks")
def api_video_chunks(video_id: int, _: str = Depends(require_access_token)) -> list[dict[str, Any]]:
    """Returns raw time-stamped transcript chunks for a video."""
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT c.id, c.start_sec, c.end_sec, c.text
            FROM chunks c
            WHERE c.video_id = ?
            ORDER BY c.chunk_index ASC
            """,
            (video_id,),
        ).fetchall()
        return [dict(row) for row in rows]


@router.get("/api/videos")
def api_list_videos(_: str = Depends(require_access_token)) -> list[dict[str, Any]]:
    """Returns list of all indexed video IDs and titles."""
    settings = get_sqlite_settings()
    with db_connection(settings) as connection:
        rows = connection.execute("SELECT id, title FROM videos ORDER BY title ASC").fetchall()
        return [dict(row) for row in rows]


@router.post("/api/chunks/{chunk_id}")
async def api_update_chunk(chunk_id: int, request: Request, _: str = Depends(require_access_token)) -> dict[str, Any]:
    """Updates a single chunk text in SQLite database, JSON transcript, and Manticore search index."""
    data = await request.json()
    new_text: str | None = data.get("text")
    if new_text is None:
        raise HTTPException(status_code=400, detail="Missing text")

    settings = get_sqlite_settings()
    embed_client = UnifiedEmbeddingClient(get_embedding_settings())
    manticore = get_manticore_client()
    q_settings = get_manticore_settings()

    with db_connection(settings) as connection:
        # 1. Fetch chunk info for payload
        row = connection.execute(
            """
            SELECT
                c.chunk_index, c.video_id, c.start_sec, c.end_sec,
                v.title, v.source_file_id, v.source_url
            FROM chunks c
            JOIN videos v ON v.id = c.video_id
            WHERE c.id = ?
            """,
            (chunk_id,),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Chunk not found")

        # 2. Update SQLite
        connection.execute("UPDATE chunks SET text = ? WHERE id = ?", (new_text, chunk_id))

        # 3. Generate new vectors
        try:
            dense_vec, sparse_vec = await embed_client.embed_text_async(new_text, task_type="RETRIEVAL_DOCUMENT")

            # 4. Update Manticore
            vectors: dict[str, Any] = {"default": dense_vec}
            if sparse_vec:
                vectors["text-sparse"] = sparse_vec

            manticore.upsert(
                collection_name=q_settings.table_name,
                points=[
                    {
                        "id": chunk_id,
                        "vector": vectors,
                        "payload": {
                            "chunk_id": chunk_id,
                            "video_id": str(row["video_id"]),
                            "chunk_index": row["chunk_index"],
                            "start_sec": row["start_sec"],
                            "end_sec": row["end_sec"],
                            "text": new_text,
                            "title": row["title"],
                            "source_file_id": row["source_file_id"],
                            "source_url": row["source_url"],
                            "is_primary": True,
                        },
                    }
                ],
            )
            vector_updated = True
        except Exception as e:
            logger.error(f"Failed to update vectors in Manticore for chunk {chunk_id}: {e}")
            vector_updated = False

        # 5. Update physical file on disk
        source_file_id = row["source_file_id"]
        if source_file_id:
            app_settings = get_app_settings()
            path = app_settings.get_normalized_transcript_path(source_file_id)
            if path.exists():
                try:
                    import gzip

                    with gzip.open(path, "rt", encoding="utf-8") as f:
                        content = json.load(f)

                    chunk_index = row["chunk_index"]
                    if "utterances" in content and len(content["utterances"]) > chunk_index:
                        content["utterances"][chunk_index]["text"] = new_text
                    elif "chunks" in content and len(content["chunks"]) > chunk_index:
                        content["chunks"][chunk_index]["text"] = new_text

                    if "utterances" in content:
                        content["transcript"] = " ".join(u["text"] for u in content["utterances"])

                    with gzip.open(path, "wt", encoding="utf-8") as f:
                        json.dump(content, f, separators=(",", ":"), ensure_ascii=False)
                except Exception as e:
                    logger.error(f"Failed to sync JSON file {path}: {e}")

    return {"status": "updated", "vector_updated": vector_updated}


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
async def api_delete_speaker(speaker_id: str, _: str = Depends(require_admin)) -> dict[str, str]:
    """Deletes registered speaker sample files and database references."""
    m_client = get_manticore_client()
    settings = get_app_settings()

    try:
        points = m_client.retrieve(collection_name="speaker_registry", ids=[speaker_id])
        if points and points[0].payload:
            filename = points[0].payload.get("sample_file")
            if filename:
                file_path = settings.voice_samples_dir / filename
                if file_path.exists():
                    file_path.unlink()
    except Exception:
        pass

    m_client.delete(collection_name="speaker_registry", ids=[speaker_id])
    return {"status": "deleted"}


@router.get("/videos/{video_id}/file")
async def video_file(video_id: int, request: Request) -> Response:
    require_access_token(request)

    pg_settings = get_sqlite_settings()
    with db_connection(pg_settings) as connection:
        row = connection.execute(
            "SELECT * FROM videos WHERE id = ?",
            (video_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404)

    if row["source_file_id"]:
        drive_client = GoogleDriveClient(get_google_drive_settings())

        # Open the stream from Google Drive
        resp = await drive_client.open_media_stream(row["source_file_id"], range_header=request.headers.get("range"))

        # Prepare headers for the browser
        headers = {
            "Accept-Ranges": "bytes",
        }
        if resp.headers.get("Content-Range"):
            headers["Content-Range"] = str(resp.headers.get("Content-Range"))

        # We DON'T pass Content-Length here to avoid mismatch errors if the stream closes early
        # or if there's any discrepancy. Browsers will handle chunked or unknown length for video tags.

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
