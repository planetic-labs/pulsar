import json
import logging
import time
from pathlib import Path
import sys

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from app.config import get_sqlite_settings, get_deepgram_settings
from app.db import db_connection
from app.transcription.postprocessing import apply_postprocessing_to_raw
from app.transcription.deepgram import DeepgramEngine
from app.chunking import chunk_from_utterances
from app.repository import replace_chunks

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def update_transcriptions():
    settings = get_sqlite_settings()
    dg_settings = get_deepgram_settings()
    engine = DeepgramEngine(dg_settings)
    
    with db_connection(settings) as conn:
        # Get all transcripts and their associated video info
        sql = """
            SELECT t.id as transcript_id, t.video_id, t.raw_json_path, t.normalized_json_path, v.title
            FROM transcripts t
            JOIN videos v ON v.id = t.video_id
        """
        transcripts = conn.execute(sql).fetchall()
        
        logger.info(f"Found {len(transcripts)} transcripts to process.")
        
        for row in transcripts:
            t_id = row["transcript_id"]
            v_id = row["video_id"]
            raw_path = Path(row["raw_json_path"])
            norm_path = Path(row["normalized_json_path"])
            title = row["title"]
            
            logger.info(f"Processing: {title} (ID: {v_id})")
            
            if not raw_path.exists():
                logger.error(f"Raw file not found: {raw_path}")
                continue
            
            try:
                # 1. Read Raw
                with open(raw_path, "r", encoding="utf-8") as f:
                    raw_payload = json.load(f)
                
                # 2. Apply Post-processing
                updated_raw = apply_postprocessing_to_raw(raw_payload)
                
                # 3. Write Updated Raw back
                with open(raw_path, "w", encoding="utf-8") as f:
                    json.dump(updated_raw, f, ensure_ascii=False, indent=2)
                
                # 4. Re-normalize
                norm_payload = engine.normalize_response(updated_raw)
                
                # 5. Write Updated Normalized
                with open(norm_path, "w", encoding="utf-8") as f:
                    json.dump(norm_payload, f, ensure_ascii=False, indent=2)
                
                # 6. Update Chunks in DB
                raw_chunks = norm_payload.get("utterances") or norm_payload.get("chunks") or []
                chunks_data = chunk_from_utterances(raw_chunks)
                
                # replace_chunks deletes old and inserts new, but we need to keep IDs if possible?
                # Actually, Qdrant IDs are derived from SQL chunk IDs. 
                # If we delete and re-insert, IDs will change UNLESS we are very careful.
                # However, our Stage 3 indexing task will clear/update Qdrant anyway.
                # The safest way to preserve search results continuity is to update existing rows.
                
                # Let's check how many chunks we have
                existing_chunks = conn.execute("SELECT id, chunk_index FROM chunks WHERE transcript_id = ?", (t_id,)).fetchall()
                
                if len(existing_chunks) == len(chunks_data):
                    # Same number of chunks, surgical update
                    for i, c_data in enumerate(chunks_data):
                        chunk_id = existing_chunks[i]["id"]
                        conn.execute(
                            "UPDATE chunks SET text = ?, speaker_tags = ? WHERE id = ?",
                            (c_data["text"], c_data.get("speaker"), chunk_id)
                        )
                else:
                    # Different number of chunks (rare if only text changed), full replace
                    logger.warning(f"Chunk count mismatch for {title} ({len(existing_chunks)} vs {len(chunks_data)}). Replacing all.")
                    replace_chunks(conn, video_id=v_id, transcript_id=t_id, chunks=chunks_data)
                
                # 7. Queue Reindex Task
                payload = json.dumps({"video_id": v_id, "title": title})
                
                # Check if task already exists
                exists = conn.execute(
                    "SELECT 1 FROM tasks WHERE task_type = 'stage_3_index' AND status IN ('pending', 'running') AND payload LIKE ?",
                    (f'%"video_id": {v_id}%',)
                ).fetchone()
                
                if not exists:
                    conn.execute(
                        "INSERT INTO tasks (task_type, payload, status, priority) VALUES (?, ?, ?, ?)",
                        ("stage_3_index", payload, "pending", 10)
                    )
                
                logger.info(f"Successfully updated and queued reindex for {title}")
                
            except Exception as e:
                logger.error(f"Failed to process {title}: {e}")
                
    logger.info("Migration complete. Don't forget to start the worker if it's not running.")

if __name__ == "__main__":
    update_transcriptions()
