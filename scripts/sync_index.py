import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import httpx

# Add project root to python path to import app modules
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_app_settings, get_google_drive_settings, get_sqlite_settings
from app.db import db_connection
from app.google_drive import GoogleDriveClient
from app.repository import extract_date_from_title, upsert_folder

LOGS_DIR = ROOT_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
log_file = LOGS_DIR / "sync_index.log"

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)

# File handler with full formatting
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
root_logger.addHandler(file_handler)

# Console handler with same detailed formatting for cron tasks monitoring
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
root_logger.addHandler(console_handler)

logger = logging.getLogger("sync_index")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


async def send_telegram_notification(missing_files: list[dict]):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram Bot Token or Chat ID not configured. Skipping notification.")
        return

    if not missing_files:
        return

    logger.info("Sending Telegram notification for missing files...")

    # Compose message
    lines = ["<b>⚠️ Обнаружены пропавшие файлы на Google Drive!</b>\n"]
    for idx, f in enumerate(missing_files, 1):
        lines.append(f"{idx}. <b>{f['title']}</b>")
        lines.append(f"   ID: <code>{f['file_id']}</code>")
        if f.get("source_url"):
            lines.append(f"   <a href='{f['source_url']}'>Ссылка на Google Drive</a>")
        lines.append("")

    message = "\n".join(lines)

    # Send in chunks of 4096 chars (Telegram API limit)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Split message if it is too long
    chunk_size = 4000
    for i in range(0, len(message), chunk_size):
        chunk = message[i : i + chunk_size]
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(url, json=payload, timeout=10.0)
                res.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send Telegram chunk: {e}")
            break


async def send_telegram_duplicate_alerts(duplicates: list[dict]):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram Bot Token or Chat ID not configured. Skipping duplicate alert.")
        return

    if not duplicates:
        return

    logger.info("Sending Telegram notification for duplicate files in database...")

    # Compose message
    lines = ["<b>⚠️ Обнаружены файлы с дублирующимися названиями в БД!</b>\n"]
    for idx, d in enumerate(duplicates, 1):
        lines.append(f"{idx}. Название: <b>{d['title']}</b>")
        lines.append(f"   Новый файл ID: <code>{d['new_file_id']}</code>")
        lines.append(f"   Существующий файл ID: <code>{d['existing_file_id']}</code>")
        lines.append("")

    message = "\n".join(lines)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    chunk_size = 4000
    for i in range(0, len(message), chunk_size):
        chunk = message[i : i + chunk_size]
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(url, json=payload, timeout=10.0)
                res.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send Telegram duplicate alert chunk: {e}")
            break


async def scan_folder_recursive(
    drive: GoogleDriveClient,
    folder_id: str,
    parent_id: str | None,
    folder_name: str,
    visited_folders: list[dict],
    drive_files: list[dict],
    exclude_keywords: tuple[str, ...],
):
    logger.info(f"Scanning folder: {folder_name} ({folder_id})")

    visited_folders.append({"id": folder_id, "name": folder_name, "parent_id": parent_id})

    try:
        items = await drive.list_folder_contents(folder_id, use_cache=False)
    except Exception as e:
        logger.error(f"Failed to list folder contents for {folder_name} ({folder_id}): {e}")
        return

    for item in items:
        if item["is_folder"]:
            await scan_folder_recursive(
                drive, item["id"], folder_id, item["name"], visited_folders, drive_files, exclude_keywords
            )
        else:
            mime_type = item.get("mime_type") or ""
            if "video/" in mime_type:
                # Check for excluded keywords (case-insensitive)
                name_upper = item["name"].upper()
                should_exclude = False
                for kw in exclude_keywords:
                    if kw.upper() in name_upper:
                        logger.info(f"Excluding file from indexing due to keyword '{kw}': {item['name']}")
                        should_exclude = True
                        break

                if not should_exclude:
                    drive_files.append(
                        {
                            "file_id": item["id"],
                            "name": item["name"],
                            "parent_folder_id": folder_id,
                            "mime_type": mime_type,
                            "md5_checksum": item.get("md5_checksum"),
                        }
                    )


async def main():
    logger.info("Starting Google Drive index synchronization...")
    sqlite_settings = get_sqlite_settings()
    drive_settings = get_google_drive_settings()
    app_settings = get_app_settings()
    drive = GoogleDriveClient(drive_settings)

    # 1. Fetch root folders and existing local data
    root_folders = []
    local_videos = {}
    active_task_file_ids = set()
    active_indexing_ids = set()

    with db_connection(sqlite_settings) as conn:
        # Get root folders from DB
        rows = conn.execute("""
            SELECT id, name FROM folders
            WHERE parent_id IS NULL OR parent_id NOT IN (SELECT id FROM folders)
        """).fetchall()
        for row in rows:
            root_folders.append({"id": row["id"], "name": row["name"]})

        # Get all local videos from DB
        videos = conn.execute("""
            SELECT id, source_file_id, title, parent_folder_id, recorded_date, is_4k, processing_status, source_url
            FROM videos WHERE source_type = 'google_drive'
        """).fetchall()
        for v in videos:
            if v["source_file_id"]:
                local_videos[v["source_file_id"]] = dict(v)

        # Get active tasks to avoid duplicate import/transcription queuing
        tasks = conn.execute(
            "SELECT payload FROM tasks WHERE task_type IN ('stage_1_download', 'stage_2_transcribe')"
        ).fetchall()
        for t in tasks:
            try:
                payload = json.loads(t["payload"])
                fid = payload.get("file_id")
                if fid:
                    active_task_file_ids.add(fid)
            except Exception:
                pass

        # Get active indexing tasks to avoid duplicate re-indexing queuing
        indexing_tasks = conn.execute(
            "SELECT payload FROM tasks WHERE task_type = 'stage_3_index' AND status IN ('pending', 'running')"
        ).fetchall()
        for t in indexing_tasks:
            try:
                payload = json.loads(t["payload"])
                vid = payload.get("video_id")
                if vid:
                    active_indexing_ids.add(vid)
            except Exception:
                pass

    if not root_folders:
        logger.warning("No root folders configured for indexing in the database. Exiting.")
        return

    logger.info(f"Loaded {len(root_folders)} root folders from DB.")
    logger.info(f"Loaded {len(local_videos)} videos from local DB.")

    # 2. Recursively scan Google Drive
    visited_folders = []
    drive_files = []

    for rf in root_folders:
        rf_id = rf["id"]
        rf_name = rf["name"]
        if not rf_name:
            try:
                folder_meta = await drive.get_file(rf_id)
                rf_name = folder_meta.name
            except Exception as e:
                logger.error(f"Could not fetch metadata for root folder {rf_id}: {e}")
                rf_name = "Root Folder"

        await scan_folder_recursive(
            drive, rf_id, None, rf_name, visited_folders, drive_files, app_settings.exclude_keywords
        )

    logger.info(f"Scan complete. Found {len(visited_folders)} folders and {len(drive_files)} videos on Google Drive.")

    # 3. Synchronize folders structure
    visited_folder_ids = {f["id"] for f in visited_folders}

    with db_connection(sqlite_settings) as conn:
        # Upsert all visited folders
        for folder in visited_folders:
            upsert_folder(conn, folder_id=folder["id"], name=folder["name"], parent_id=folder["parent_id"])

        # Delete folders that are no longer present on Google Drive
        db_folders = conn.execute("SELECT id FROM folders").fetchall()
        db_folder_ids = {row["id"] for row in db_folders}

        folders_to_delete = db_folder_ids - visited_folder_ids
        if folders_to_delete:
            logger.info(f"Deleting {len(folders_to_delete)} removed folders from local DB...")
            placeholders = ",".join("?" for _ in folders_to_delete)
            conn.execute(f"DELETE FROM folders WHERE id IN ({placeholders})", list(folders_to_delete))

    # 4. Synchronize video files and metadata
    drive_file_ids = {f["file_id"] for f in drive_files}
    new_files_to_queue = []
    duplicate_alerts = []

    # Map existing titles to their source_file_id to check for duplicates
    title_to_file_id = {v["title"]: v["source_file_id"] for v in local_videos.values() if v["title"]}

    with db_connection(sqlite_settings) as conn:
        for df in drive_files:
            file_id = df["file_id"]
            name = df["name"]
            parent_id = df["parent_folder_id"]

            # Check if this filename is already indexed under another file_id
            existing_fid = title_to_file_id.get(name)
            if existing_fid and existing_fid != file_id:
                duplicate_alerts.append({"title": name, "new_file_id": file_id, "existing_file_id": existing_fid})

            # Compute metadata dates and 4K flag
            recorded_date = extract_date_from_title(name)
            is_4k = bool(re.search(r"4[KК]", name))

            if file_id in local_videos:
                # File exists locally, check if it needs sync
                lv = local_videos[file_id]

                # Check for updates in title, parent folder, recorded date or is_4k
                if (
                    lv["title"] != name
                    or lv["parent_folder_id"] != parent_id
                    or lv["recorded_date"] != recorded_date
                    or bool(lv["is_4k"]) != is_4k
                ):
                    logger.info(f"Updating local metadata for video {file_id}: '{lv['title']}' -> '{name}'")
                    conn.execute(
                        """
                        UPDATE videos
                        SET title = ?, parent_folder_id = ?, recorded_date = ?,
                            is_4k = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE source_file_id = ?
                        """,
                        (name, parent_id, recorded_date, int(is_4k), file_id),
                    )

                    # If it was already indexed, re-queue stage_3_index to update metadata in Qdrant
                    if lv["processing_status"] in ("indexed_chunks_ready", "transcribed", "indexing"):
                        if lv["id"] not in active_indexing_ids:
                            conn.execute(
                                "INSERT INTO tasks (task_type, payload, status, priority) VALUES (?, ?, ?, ?)",
                                (
                                    "stage_3_index",
                                    json.dumps({"video_id": lv["id"], "title": name}, ensure_ascii=False),
                                    "pending",
                                    5,
                                ),
                            )
                            logger.info(f"Queued re-indexing task to sync Qdrant metadata for video: {name}")
            else:
                # New file found, check if it is already in active download tasks to avoid duplicates
                if file_id not in active_task_file_ids:
                    new_files_to_queue.append(df)

    # 5. Queue new files for indexing
    if new_files_to_queue:
        logger.info(f"Queuing {len(new_files_to_queue)} new files for import/indexing...")
        with db_connection(sqlite_settings) as conn:
            for file_info in new_files_to_queue:
                file_id = file_info["file_id"]
                name = file_info["name"]

                conn.execute(
                    "INSERT INTO tasks (task_type, payload, status) VALUES (?, ?, ?)",
                    (
                        "stage_1_download",
                        json.dumps({"file_id": file_id, "title": name, "diarize": True}, ensure_ascii=False),
                        "pending",
                    ),
                )
                logger.info(f"Queued download task for file: {name} ({file_id})")

    # 6. Check for missing files (present locally but missing on Google Drive)
    missing_files = []
    for file_id, lv in local_videos.items():
        # Only notify about successfully processed or indexing videos, ignore failed ones
        if file_id not in drive_file_ids and lv["processing_status"] not in ("failed", "skipped_silent"):
            missing_files.append({"file_id": file_id, "title": lv["title"], "source_url": lv["source_url"]})
            logger.warning(f"File missing on Google Drive: {lv['title']} ({file_id})")

    # 7. Send Telegram Notifications
    if missing_files:
        await send_telegram_notification(missing_files)

    if duplicate_alerts:
        await send_telegram_duplicate_alerts(duplicate_alerts)

    # 8. Check pending tasks and start the background worker if not already running
    try:
        with db_connection(sqlite_settings) as conn:
            pending_count = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status = 'pending'").fetchone()[
                "cnt"
            ]

        if pending_count > 0:
            logger.info(f"Found {pending_count} pending tasks in queue. Checking worker status...")
            base_url = f"http://127.0.0.1:{app_settings.port}"
            headers = {"Authorization": f"Bearer {app_settings.access_token}"}

            async with httpx.AsyncClient(timeout=5.0) as client:
                try:
                    status_res = await client.get(f"{base_url}/api/v1/worker/status", headers=headers)
                    if status_res.status_code == 200:
                        status_data = status_res.json()
                        if not status_data.get("is_running"):
                            logger.info("Worker is not running. Triggering worker start...")
                            start_res = await client.post(f"{base_url}/api/v1/worker/start", headers=headers)
                            if start_res.status_code == 200:
                                logger.info(f"Worker start triggered successfully: {start_res.json()}")
                            else:
                                logger.warning(f"Failed to start worker: HTTP {start_res.status_code}")
                        else:
                            logger.info("Worker is already running.")
                    else:
                        logger.warning(f"Failed to get worker status: HTTP {status_res.status_code}")
                except httpx.RequestError as req_err:
                    logger.warning(f"Could not connect to Pulsar API to check/start worker: {req_err}")
    except Exception as e:
        logger.error(f"Error checking/starting worker: {e}")

    logger.info("Synchronization complete.")


if __name__ == "__main__":
    asyncio.run(main())
