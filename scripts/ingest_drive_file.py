import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from qdrant_client import models

from app.audio import extract_audio
from app.config import (
    get_app_settings,
    get_deepgram_settings,
    get_embedding_settings,
    get_google_drive_settings,
    get_qdrant_settings,
    get_sqlite_settings,
)
from app.db import db_connection, init_db
from app.gemini import UnifiedEmbeddingClient
from app.google_drive import GoogleDriveClient
from app.qdrant import get_qdrant_client
from app.repository import (
    check_transcript_exists,
    replace_chunks,
    replace_transcript,
    update_video_status,
    upsert_video,
)
from app.transcription.deepgram import DeepgramEngine
from app.voice import extract_speaker_embedding

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def ingest_drive_file(
    file_id: str,
    title: str | None = None,
    diarize: bool = True,
    clip_duration_sec: float | None = None,
    clip_start_sec: float = 0.0,
    download_progress_callback: Callable[[int, int], None] | None = None,
    status_callback: Callable[[str], None] | None = None,
    keep_files: bool = False,
) -> dict[str, Any]:

    def set_status(msg: str):
        if status_callback:
            status_callback(msg)
        logger.info(msg)

    drive_settings = get_google_drive_settings()
    app_settings = get_app_settings()
    pg_settings = get_sqlite_settings()

    drive = GoogleDriveClient(drive_settings)
    file_meta = drive.get_file(file_id)

    set_status(f"[1/6] Подготовка: '{file_meta.name}'")

    with db_connection(pg_settings) as connection:
        init_db(connection)
        existing = connection.execute(
            "SELECT id, processing_status FROM videos WHERE source_file_id = ?", (file_id,)
        ).fetchone()

    dg_settings = get_deepgram_settings()
    engine_id = f"deepgram:{dg_settings.model}"

    if existing and existing["processing_status"] == "indexed_chunks_ready":
        with db_connection(pg_settings) as connection:
            if check_transcript_exists(connection, int(existing["id"])):
                set_status(f"--- Файл {file_id} уже проиндексирован.")
                return {"video_id": int(existing["id"]), "status": "already_indexed"}

    video_path = app_settings.storage_dir / "downloads" / f"{file_id}.mp4"
    audio_path = app_settings.storage_dir / "audio" / f"{file_id}.wav"

    video_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Download
    if not video_path.exists():
        set_status("[2/6] Скачивание из Google Drive...")
        drive.download_file(file_id, video_path, progress_callback=download_progress_callback)
    else:
        set_status("[2/6] Видео уже есть локально.")

    # 2. Extract Audio
    if not audio_path.exists():
        set_status("[3/6] Извлечение аудио (FFmpeg)...")
        extract_audio(video_path, audio_path)
    else:
        set_status("[3/6] Аудио-файл уже существует.")

    # 3. Transcribe
    set_status("[4/6] Транскрибация (Deepgram)...")
    engine = DeepgramEngine(dg_settings)

    raw_filename = f"{engine_id.replace(':', '_')}_{int(time.time())}.json"
    raw_path = app_settings.raw_transcripts_dir / file_id / raw_filename
    norm_filename = f"{file_id}_{engine_id.replace(':', '_')}.json"
    norm_path = app_settings.normalized_transcripts_dir / norm_filename

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    norm_path.parent.mkdir(parents=True, exist_ok=True)

    if norm_path.exists():
        set_status("--- Использование кэша транскрипции.")
        normalized_payload = json.loads(norm_path.read_text(encoding="utf-8"))
    else:
        raw_payload = engine.transcribe_file(audio_path, diarize=diarize)
        normalized_payload = engine.normalize_response(raw_payload)

        raw_path.write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        norm_path.write_text(json.dumps(normalized_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4. Indexing
    set_status("[5/6] Индексация в Qdrant...")
    embed_client = UnifiedEmbeddingClient(get_embedding_settings())
    q_settings = get_qdrant_settings()
    qdrant = get_qdrant_client()

    with db_connection(pg_settings) as connection:
        video_id = upsert_video(
            connection,
            source_type="google_drive",
            source_file_id=file_id,
            title=title or file_meta.name,
            source_url=f"https://drive.google.com/file/d/{file_id}/view",
            mime_type=file_meta.mime_type,
            size_bytes=None,
            duration_sec=None,
            local_video_path=str(video_path),
            local_audio_path=str(audio_path),
            processing_status="transcribing",
        )

        transcript_id = replace_transcript(
            connection,
            video_id=video_id,
            language="ru",  # Added missing language
            confidence=None,
            raw_json_path=Path(raw_path),
            normalized_json_path=Path(norm_path),
        )

        chunks_data = normalized_payload.get("utterances") or normalized_payload.get("chunks")
        if not chunks_data:
            chunks_data = []

        replace_chunks(
            connection,
            video_id=video_id,
            transcript_id=transcript_id,
            chunks=chunks_data,
        )

        rows = connection.execute(
            """
            SELECT id, chunk_index, start_sec, end_sec, text, speaker_tags
            FROM chunks WHERE transcript_id = ? ORDER BY chunk_index ASC
            """,
            (transcript_id,),
        ).fetchall()

        if rows:
            texts = [row["text"] for row in rows]
            try:
                set_status(f"--- Генерация эмбеддингов ({len(rows)} фрагментов)...")
                # One call gets BOTH dense and sparse vectors via API
                embeddings_data = embed_client.embed_batch(texts)

                points: list[models.PointStruct] = []
                for idx, row in enumerate(rows):
                    dense_vec, sparse_vec = embeddings_data[idx]

                    vectors: dict[str, Any] = {"default": dense_vec}
                    if sparse_vec:
                        vectors["text-sparse"] = sparse_vec

                    points.append(
                        models.PointStruct(
                            id=row["id"],
                            vector=vectors,
                            payload={
                                "chunk_id": row["id"],
                                "video_id": video_id,
                                "transcript_id": transcript_id,
                                "chunk_index": row["chunk_index"],
                                "start_sec": row["start_sec"],
                                "end_sec": row["end_sec"],
                                "text": row["text"],
                                "speaker": row["speaker_tags"],
                                "title": title or file_meta.name,
                                "source_file_id": file_id,
                                "source_url": f"https://drive.google.com/file/d/{file_id}/view",
                                "is_primary": True,
                            },
                        )
                    )

                if points:
                    qdrant.upsert(collection_name=q_settings.collection_name, points=points)
            except Exception as e:
                logger.error(f"Ошибка индексации: {e}")
                raise e

        # 6. Automatic Speaker Recognition
        if diarize:
            set_status("[6/6] Распознавание спикеров...")

            speaker_samples: dict[str, list[Any]] = {}
            for row in rows:
                if row["speaker_tags"]:
                    for tag in row["speaker_tags"].split(", "):
                        if tag not in speaker_samples:
                            speaker_samples[tag] = []
                        speaker_samples[tag].append(row)

            threshold = 0.96
            for tag, chunks_list in speaker_samples.items():
                if not chunks_list:
                    continue
                best_chunk = max(chunks_list, key=lambda x: float(x["end_sec"]) - float(x["start_sec"]))
                start, end = float(best_chunk["start_sec"]), float(best_chunk["end_sec"])

                try:
                    embedding = extract_speaker_embedding(audio_path, start, end)
                    if embedding:
                        search_results = qdrant.query_points(
                            collection_name="speaker_registry", query=embedding, limit=1
                        ).points
                        if search_results and float(search_results[0].score) >= threshold:
                            res_payload = search_results[0].payload
                            name = res_payload.get("name") if res_payload else None
                            connection.execute(
                                """
                                INSERT INTO speakers (video_id, speaker_tag, name)
                                VALUES (?, ?, ?) ON CONFLICT DO NOTHING
                                """,
                                (video_id, tag, name),
                            )

                except Exception:
                    pass

        # Final Status Update
        update_video_status(connection, video_id=video_id, processing_status="indexed_chunks_ready")

        # Cleanup
        if not keep_files:
            try:
                if video_path.exists():
                    video_path.unlink()
                connection.execute("UPDATE videos SET local_video_path = NULL WHERE id = ?", (video_id,))
            except Exception:
                pass

    set_status(f"=== ГОТОВО: {file_meta.name} ===")
    return {"video_id": video_id, "chunks_count": len(chunks_data)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file_id")
    parser.add_argument("--diarize", action="store_true")
    args = parser.parse_args()
    ingest_drive_file(args.file_id, diarize=args.diarize)
