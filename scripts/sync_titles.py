import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_google_drive_settings, get_sqlite_settings
from app.db import db_connection
from app.google_drive import GoogleDriveClient
from app.repository import upsert_folder

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def upsert_folder_chain_async(drive: GoogleDriveClient, connection: Any, folder_id: str):
    """Recursively upsert folder chain to DB. Reused from ingest script."""
    try:
        f_meta = await drive.get_file(folder_id)
        parent_id = f_meta.parents[0] if f_meta.parents else None
        upsert_folder(connection, folder_id=folder_id, name=f_meta.name, parent_id=parent_id)

        if parent_id and parent_id != "root":
            # Check if parent exists to avoid redundant API calls
            exists = connection.execute("SELECT 1 FROM folders WHERE id = ?", (parent_id,)).fetchone()
            if not exists:
                await upsert_folder_chain_async(drive, connection, parent_id)
    except Exception as e:
        logger.warning(f"Error in upsert_folder_chain_async for {folder_id}: {e}")


async def sync_indexed_metadata():
    """
    Synchronizes titles and folder hierarchy for all indexed Google Drive files.
    Also updates folder names if they have changed.
    """
    pg_settings = get_sqlite_settings()
    drive_settings = get_google_drive_settings()
    drive_client = GoogleDriveClient(drive_settings)

    with db_connection(pg_settings) as conn:
        # 1. Sync Folders (names and parent hierarchy)
        folder_rows = conn.execute("SELECT id, name FROM folders").fetchall()
        logger.info(f"Checking {len(folder_rows)} folders...")
        for f_row in folder_rows:
            try:
                f_id = f_row["id"]
                drive_f = await drive_client.get_file(f_id)
                if drive_f.name != f_row["name"]:
                    conn.execute("UPDATE folders SET name = ? WHERE id = ?", (drive_f.name, f_id))
                    logger.info(f"Folder renamed: {f_id} -> {drive_f.name}")
            except Exception as e:
                logger.error(f"Failed to sync folder {f_row['id']}: {e}")

        # 2. Sync Videos (titles and parent_folder_id)
        sql_v = """
            SELECT id, source_file_id, title, parent_folder_id, duration_sec
            FROM videos
        """
        rows = conn.execute(sql_v).fetchall()

        if not rows:
            logger.info("No videos found to sync.")
            return 0

        logger.info(f"Syncing metadata for {len(rows)} videos...")

        updated_count = 0
        for row in rows:
            video_id = row["id"]
            file_id = row["source_file_id"]
            current_title = row["title"]
            current_parent = row["parent_folder_id"]

            try:
                # Get fresh metadata
                drive_file = await drive_client.get_file(file_id)
                new_title = drive_file.name
                new_parent = drive_file.parents[0] if drive_file.parents else None

                # Fetch duration from normalized JSON if missing in DB
                new_duration = row["duration_sec"]
                if not new_duration:
                    # Try to find transcript for this video to get duration
                    sql_t = "SELECT normalized_json_path FROM transcripts WHERE video_id = ?"
                    t_row = conn.execute(sql_t, (video_id,)).fetchone()
                    if t_row and t_row["normalized_json_path"]:
                        t_path = Path(t_row["normalized_json_path"])
                        if t_path.exists():
                            try:
                                t_data = json.loads(t_path.read_text(encoding="utf-8"))
                                new_duration = t_data.get("duration")
                            except Exception:
                                pass

                needs_update = False
                if new_title and new_title != current_title:
                    needs_update = True
                if new_parent != current_parent:
                    needs_update = True
                    # If parent changed, ensure new parent chain is indexed
                    if new_parent:
                        await upsert_folder_chain_async(drive_client, conn, new_parent)

                if new_duration and new_duration != row["duration_sec"]:
                    needs_update = True

                if needs_update:
                    sql = """
                        UPDATE videos
                        SET title = ?, parent_folder_id = ?, duration_sec = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """
                    conn.execute(sql, (new_title, new_parent, new_duration, video_id))
                    logger.info(f"Updated video {file_id}: title='{new_title}', duration={new_duration}")
                    updated_count += 1

            except Exception as e:
                logger.error(f"Failed to fetch metadata for video {file_id}: {e}")

        return updated_count


if __name__ == "__main__":
    count = asyncio.run(sync_indexed_metadata())
    print(f"Sync completed. Updated {count} items.")
