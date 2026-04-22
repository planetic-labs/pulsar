import logging
from pathlib import Path
import sys

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_sqlite_settings, get_google_drive_settings
from app.db import db_connection
from app.google_drive import GoogleDriveClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def sync_titles():
    pg_settings = get_sqlite_settings()
    drive_settings = get_google_drive_settings()
    drive_client = GoogleDriveClient(drive_settings)

    with db_connection(pg_settings) as conn:
        # Находим видео, где заголовок похож на ID (длинная строка без пробелов) или просто требует обновления
        rows = conn.execute(
            "SELECT id, source_file_id, title FROM videos WHERE source_type = 'google_drive'"
        ).fetchall()
        
        if not rows:
            logger.info("No videos found to sync.")
            return

        logger.info(f"Checking titles for {len(rows)} videos...")
        
        updated_count = 0
        for row in rows:
            video_id = row["id"]
            file_id = row["source_file_id"]
            current_title = row["title"]
            
            try:
                # Получаем свежие метаданные из Google Drive
                drive_file = drive_client.get_file(file_id)
                new_title = drive_file.name
                
                if new_title and new_title != current_title:
                    conn.execute(
                        "UPDATE videos SET title = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                        (new_title, video_id)
                    )
                    conn.commit()
                    logger.info(f"Updated: {file_id} -> {new_title}")
                    updated_count += 1
                else:
                    logger.debug(f"Skipped: {current_title} is already correct.")
                    
            except Exception as e:
                logger.error(f"Failed to fetch metadata for {file_id}: {e}")

        logger.info(f"Sync completed. Updated {updated_count} titles.")

if __name__ == "__main__":
    sync_titles()
