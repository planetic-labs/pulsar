import asyncio
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_google_drive_settings, get_sqlite_settings
from app.db import db_connection
from app.google_drive import GoogleDriveClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def backfill_md5_checksums():
    """
    Fetches missing md5Checksums from Google Drive for all indexed videos
    and updates the database.
    """
    pg_settings = get_sqlite_settings()
    drive_settings = get_google_drive_settings()
    drive_client = GoogleDriveClient(drive_settings)

    with db_connection(pg_settings) as conn:
        # Find videos that don't have md5_checksum yet
        sql_v = """
            SELECT id, source_file_id, title
            FROM videos
            WHERE source_type = 'google_drive'
              AND (md5_checksum IS NULL OR md5_checksum = '')
        """
        rows = conn.execute(sql_v).fetchall()

        if not rows:
            logger.info("No videos found needing MD5 backfill.")
            return 0

        logger.info(f"Found {len(rows)} videos needing MD5 backfill. Starting...")

        updated_count = 0
        for row in rows:
            video_id = row["id"]
            file_id = row["source_file_id"]
            title = row["title"]

            try:
                # Get fresh metadata from Google Drive
                drive_file = await drive_client.get_file(file_id)
                md5 = drive_file.md5_checksum

                if md5:
                    conn.execute(
                        "UPDATE videos SET md5_checksum = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (md5, video_id),
                    )
                    logger.info(f"Updated MD5 for video {video_id} ('{title}'): {md5}")
                    updated_count += 1
                else:
                    logger.warning(f"Google Drive did not return MD5 for video {video_id} ('{title}').")

            except Exception as e:
                if "404" in str(e):
                    logger.warning(f"Video {video_id} ('{title}') not found on Drive (ID: {file_id}). Skipping.")
                else:
                    logger.error(f"Failed to fetch MD5 for video {video_id} ({file_id}): {e}")

            # Small delay to avoid aggressive API rate limiting
            await asyncio.sleep(0.1)

        return updated_count


if __name__ == "__main__":
    count = asyncio.run(backfill_md5_checksums())
    print(f"\nBackfill completed. Updated {count} videos.")
