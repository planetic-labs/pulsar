from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_app_settings, get_deepgram_settings, get_google_drive_settings, get_sqlite_settings
from app.db import db_connection
from app.google_drive import DriveFile, GoogleDriveClient
from app.repository import check_transcript_exists, get_video_by_source_file_id
from scripts.ingest_drive_file import ingest_drive_file


def safe_print(msg: str):
    """Prints and flushes immediately."""
    print(msg)
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive Google Drive Ingestion")
    parser.add_argument("folder_id", help="Google Drive folder ID")
    parser.add_argument("--limit", type=int, default=50, help="Max files to process")
    parser.add_argument("--force", action="store_true", help="Re-process indexed files")
    args = parser.parse_args()

    get_app_settings()
    pg_settings = get_sqlite_settings()
    drive = GoogleDriveClient(get_google_drive_settings())

    dg_settings = get_deepgram_settings()
    engine_id = f"deepgram:{dg_settings.model}"

    safe_print(f"\n--- Folder Selection (Target: {engine_id}) ---")
    files = drive.list_folder_files(args.folder_id, mime_prefix="video/")
    if not files:
        safe_print("No video files found.")
        return

    # Filter only new or forced
    to_process: list[DriveFile] = []
    with db_connection(pg_settings) as conn:
        for f in files:
            existing_video = get_video_by_source_file_id(conn, source_type="google_drive", source_file_id=f.file_id)

            if not existing_video:
                to_process.append(f)
            else:
                # Video exists, but maybe this transcript doesn't?
                if not check_transcript_exists(conn, existing_video["id"]) or args.force:
                    to_process.append(f)

    to_process = to_process[: args.limit]
    if not to_process:
        safe_print("All files already indexed. Use --force to re-index.")
        return

    safe_print(f"Found {len(to_process)} new file(s) to process.")
    for i, f in enumerate(to_process, 1):
        safe_print(f"[{i}/{len(to_process)}] Processing: {f.name} ({f.file_id})")
        try:
            ingest_drive_file(f.file_id)
            safe_print(f"  DONE: {f.name}")
        except Exception as e:
            safe_print(f"  FAILED: {f.name} - {str(e)}")

    safe_print("\nBatch Ingestion Finished.")


if __name__ == "__main__":
    main()
