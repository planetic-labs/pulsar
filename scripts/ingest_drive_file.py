from __future__ import annotations

import argparse
import json
import time
import logging
import re
from pathlib import Path
import sys
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.audio import extract_audio
from app.chunking import chunk_from_utterances
from app.config import (
    get_app_settings, 
    get_google_drive_settings, 
    get_sqlite_settings,
    get_deepgram_settings
)
from app.db import db_connection, init_db
from app.google_drive import GoogleDriveClient
from app.repository import (
    get_video_by_source_file_id,
    replace_chunks,
    replace_transcript,
    update_video_status,
    upsert_video,
)
from app.qdrant import get_qdrant_client, get_sparse_embedding_model, init_qdrant
from qdrant_client import models
from app.config import get_qdrant_settings
from app.transcription.deepgram import DeepgramEngine

logger = logging.getLogger(__name__)

def ingest_drive_file(
    file_id: str,
    *,
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
        existing = get_video_by_source_file_id(
            connection,
            source_type="google_drive",
            source_file_id=file_id,
        )
    
    dg_settings = get_deepgram_settings()
    engine_id = f"deepgram:{dg_settings.model}"

    if existing and existing.get("processing_status") == "indexed_chunks_ready":
        from app.repository import check_transcript_exists
        with db_connection(pg_settings) as connection:
            if check_transcript_exists(connection, existing["id"]):
                set_status(f"--- Файл {file_id} уже проиндексирован.")
                return {"video_id": int(existing["id"]), "status": "already_indexed"}

    # Paths
    clean_name = re.sub(r'[^а-яА-ЯёЁa-zA-Z0-9._-]', '_', file_meta.name)
    base_name = clean_name[:60]
    extension = Path(file_meta.name).suffix or ".mp4"
    safe_name = f"{base_name}_{file_id}{extension}"
    
    video_path = drive_settings.download_dir / safe_name
    audio_path = ROOT_DIR / "audio" / f"{base_name}_{file_id}.wav"
    
    # 1. Download
    if not video_path.exists():
        set_status(f"[2/6] Скачивание из Google Drive...")
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
    set_status(f"[4/6] Транскрибация (Deepgram)...")
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
    from app.gemini import GeminiEmbeddingClient
    from app.config import get_gemini_settings
    
    set_status("[5/6] Индексация в Qdrant...")
    embed_client = GeminiEmbeddingClient(get_gemini_settings())
    sparse_model = get_sparse_embedding_model()
    q_settings = get_qdrant_settings()
    init_qdrant()
    qdrant = get_qdrant_client()

    chunks = chunk_from_utterances(normalized_payload.get("utterances", []))
    
    with db_connection(pg_settings) as connection:
        video_id = upsert_video(
            connection,
            source_type="google_drive",
            source_file_id=file_id,
            title=title or file_meta.name,
            source_url=f"https://drive.google.com/file/d/{file_id}/view",
            mime_type=file_meta.mime_type,
            size_bytes=int(file_meta.size) if file_meta.size else None,
            duration_sec=None,
            local_video_path=str(video_path),
            local_audio_path=str(audio_path),
            processing_status="transcribing", 
        )
        
        transcript_id = replace_transcript(
            connection,
            video_id=video_id,
            language="ru",
            confidence=normalized_payload.get("confidence", 1.0),
            raw_json_path=raw_path,
            normalized_json_path=norm_path,
        )
        
        replace_chunks(
            connection,
            video_id=video_id,
            transcript_id=transcript_id,
            chunks=chunks,
        )
        
        rows = connection.execute(
            "SELECT id, chunk_index, start_sec, end_sec, text, speaker_tags FROM chunks WHERE transcript_id = ? ORDER BY chunk_index ASC",
            (transcript_id,)
        ).fetchall()

        if rows:
            texts = [row["text"] for row in rows]
            try:
                set_status(f"--- Генерация эмбеддингов ({len(rows)} фрагментов)...")
                dense_vectors = embed_client.embed_batch(texts)
                sparse_vectors_gen = list(sparse_model.embed(texts))
                
                points = []
                for idx, row in enumerate(rows):
                    sparse_gen = sparse_vectors_gen[idx]
                    sparse_vec = models.SparseVector(
                        indices=sparse_gen.indices.tolist(),
                        values=sparse_gen.values.tolist()
                    )
                    
                    points.append(models.PointStruct(
                        id=row["id"],
                        vector={"default": dense_vectors[idx], "text-sparse": sparse_vec},
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
                            "engine": engine_id,
                            "is_primary": True
                        }
                    ))
                
                if points:
                    qdrant.upsert(collection_name=q_settings.collection_name, points=points)
            except Exception as e:
                logger.error(f"Ошибка индексации: {e}")
                raise e

        # 6. Automatic Speaker Recognition
        if diarize:
            set_status("[6/6] Распознавание спикеров...")
            from app.voice import extract_speaker_embedding
            
            speaker_samples = {} 
            for row in rows:
                if row["speaker_tags"]:
                    for tag in row["speaker_tags"].split(", "):
                        if tag not in speaker_samples: speaker_samples[tag] = []
                        speaker_samples[tag].append(row)

            threshold = 0.96
            for tag, chunks_list in speaker_samples.items():
                if not chunks_list: continue
                best_chunk = max(chunks_list, key=lambda x: x["end_sec"] - x["start_sec"])
                start, end = best_chunk["start_sec"], best_chunk["end_sec"]
                
                try:
                    embedding = extract_speaker_embedding(audio_path, start, end)
                    if embedding:
                        results = qdrant.query_points(collection_name="speaker_registry", query=embedding, limit=1).points
                        if results and results[0].score >= threshold:
                            connection.execute("INSERT INTO speakers (video_id, speaker_tag, name) VALUES (?, ?, ?) ON CONFLICT DO NOTHING", (video_id, tag, results[0].payload.get("name")))
                except Exception: pass

        # Final Status Update
        update_video_status(connection, video_id=video_id, processing_status="indexed_chunks_ready")

        # Cleanup
        if not keep_files:
            try:
                if video_path.exists(): video_path.unlink()
                connection.execute("UPDATE videos SET local_video_path = NULL WHERE id = ?", (video_id,))
            except Exception: pass

    set_status(f"=== ГОТОВО: {file_meta.name} ===")
    return {"video_id": video_id, "chunks_count": len(chunks)}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file_id")
    parser.add_argument("--diarize", action="store_true")
    args = parser.parse_args()
    ingest_drive_file(args.file_id, diarize=args.diarize)
