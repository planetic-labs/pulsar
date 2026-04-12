import json
import logging
import time
import argparse
from pathlib import Path
import sys

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_postgres_settings
from app.db import db_connection, init_db
from app.search import GoogleEmbeddingClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Google AI Free Tier Limits: 100 RPM, 30,000 TPM
# Being VERY conservative to avoid 429
MAX_RPM = 50  
MAX_TPM = 20000 
CHAR_TO_TOKEN_RATIO = 0.3  # Rough estimation for Russian

def rebuild_semantic_index(full_reindex: bool = False):
    pg_settings = get_postgres_settings()
    client = GoogleEmbeddingClient()

    with db_connection(pg_settings) as conn:
        init_db(conn)
        
        if full_reindex:
            logger.info("Full reindex requested. Clearing existing embeddings...")
            conn.execute("UPDATE chunks SET embedding = NULL")
            conn.commit()
        else:
            # Упрощенная очистка: обнуляем все векторы, которые имеют нулевую длину (ошибочные)
            # В pgvector норма вектора 0 - это признак того, что он пустой
            logger.info("Cleaning up invalid embeddings from previous runs...")
            conn.execute("UPDATE chunks SET embedding = NULL WHERE embedding IS NOT NULL AND vector_norm(embedding) = 0")
            conn.commit()

        # Автоматически синхронизируем названия перед началом, чтобы в логах были имена, а не ID
        try:
            from scripts.sync_titles import sync_titles
            sync_titles()
        except Exception as e:
            logger.warning(f"Could not sync titles before indexing: {e}")

        # Find chunks without embeddings
        rows = conn.execute(
            "SELECT id, text FROM chunks WHERE embedding IS NULL ORDER BY id ASC"
        ).fetchall()
        
        if not rows:
            logger.info("No chunks to index (all chunks already have embeddings).")
            return

        total_rows = len(rows)
        logger.info(f"Generating embeddings for {total_rows} chunks using Google AI...")
        
        # Rate limiting state
        requests_in_window = 0
        tokens_in_window = 0
        window_start = time.time()

        for idx, row in enumerate(rows):
            text = row["text"]
            if not text or len(text.strip()) < 2:
                continue

            estimated_tokens = len(text) * CHAR_TO_TOKEN_RATIO
            
            # 1. Check RPM and TPM limits
            current_time = time.time()
            if current_time - window_start >= 60:
                requests_in_window = 0
                tokens_in_window = 0
                window_start = current_time

            if requests_in_window >= MAX_RPM or (tokens_in_window + estimated_tokens) >= MAX_TPM:
                sleep_time = max(0, 60 - (current_time - window_start)) + 2
                logger.warning(f"Rate limit approaching. Sleeping for {sleep_time:.1f}s...")
                time.sleep(sleep_time)
                window_start = time.time()
                requests_in_window = 0
                tokens_in_window = 0

            # 2. Perform Request with Retry Logic
            retries = 5
            success = False
            while retries > 0:
                try:
                    embedding = client.embed_text(text, is_query=False)
                    conn.execute(
                        "UPDATE chunks SET embedding = %s WHERE id = %s",
                        (embedding, row["id"])
                    )
                    conn.commit() 
                    
                    requests_in_window += 1
                    tokens_in_window += estimated_tokens
                    success = True
                    break
                    
                except Exception as e:
                    error_msg = str(e)
                    if "429" in error_msg:
                        wait_time = 65 - (time.time() - window_start)
                        if wait_time < 10: wait_time = 60
                        logger.error(f"Google Rate Limit hit (429). Cooling down for {wait_time:.1f}s... (Retries left: {retries})")
                        time.sleep(wait_time)
                        window_start = time.time()
                        requests_in_window = 0
                        tokens_in_window = 0
                        retries -= 1
                    else:
                        logger.error(f"Failed to index chunk {row['id']}: {e}")
                        # Move to next chunk on non-429 errors
                        break
            
            if success and idx > 0 and idx % 10 == 0:
                logger.info(f"Progress: {idx}/{total_rows} (Window: {requests_in_window} req, {int(tokens_in_window)} tokens)")
            
            # Smoothing delay to stay under RPM
            time.sleep(1.2) # Extra safety buffer

    logger.info("Indexing completed successfully.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reindex search using Google Embeddings")
    parser.add_argument("--full", action="store_true", help="Clear all existing embeddings and start from scratch")
    args = parser.parse_args()

    rebuild_semantic_index(full_reindex=args.full)
