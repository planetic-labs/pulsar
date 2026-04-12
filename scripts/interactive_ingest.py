from __future__ import annotations

import argparse
import sys
import threading
import queue
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import (
    get_google_drive_settings, 
    get_app_settings, 
    get_postgres_settings, 
    get_transcription_settings,
    get_deepgram_settings
)
from app.google_drive import GoogleDriveClient, DriveFile
from scripts.ingest_drive_file import ingest_drive_file
from app.db import db_connection, init_db
from app.file_dedupe import dedupe_gallery_variants
from app.repository import get_video_by_source_file_id, check_transcript_exists


# Thread-safe print lock
print_lock = threading.Lock()

def safe_print(msg: str):
    with print_lock:
        print(msg)


def draw_progress_bar(current: int, total: int, prefix: str = "", length: int = 30):
    percent = (current / total)
    filled_length = int(length * percent)
    bar = "█" * filled_length + "-" * (length - filled_length)
    with print_lock:
        sys.stdout.write(f"\r{prefix} |{bar}| {percent:.1%} ({current/(1024*1024):.1f}/{total/(1024*1024):.1f} MB)")
        sys.stdout.flush()
        if current >= total:
            sys.stdout.write("\n")


def list_folders(drive: GoogleDriveClient, parent_id: str | None = None) -> list[DriveFile]:
    query_parts = ["mimeType = 'application/vnd.google-apps.folder'", "trashed = false"]
    if parent_id:
        query_parts.append(f"'{parent_id}' in parents")
    
    query = " and ".join(query_parts)
    response = drive._list_files_page(
        page_size=50,
        query_filter=query,
        order_by="name asc"
    )
    return drive._to_drive_files(response.get("files", []))


def download_worker(drive_client: GoogleDriveClient, download_q: queue.Queue, process_q: queue.Queue, worker_id: int):
    while True:
        try:
            file_meta = download_q.get(timeout=3)
        except queue.Empty:
            break
        
        safe_print(f"[DW-{worker_id}] Downloading: {file_meta.name}")
        
        def cb(cur, tot):
            draw_progress_bar(cur, tot, prefix=f"[DW-{worker_id}] {file_meta.name[:15]}...")

        try:
            settings = get_google_drive_settings()
            from urllib.parse import quote
            safe_name = quote(file_meta.name, safe="._-() ").replace("%20", "_")
            video_path = settings.download_dir / safe_name
            
            if not video_path.exists():
                drive_client.download_file(file_meta.file_id, video_path, progress_callback=cb)
            
            process_q.put((file_meta, video_path))
            safe_print(f"[DW-{worker_id}] Finished: {file_meta.name}")
        except Exception as e:
            safe_print(f"[DW-{worker_id}] FAILED: {file_meta.name}: {e}")
        finally:
            download_q.task_done()


def process_worker(process_q: queue.Queue, worker_id: int):
    while True:
        try:
            file_meta, video_path = process_q.get(timeout=5)
        except queue.Empty:
            break
        
        safe_print(f"[PW-{worker_id}] Processing: {file_meta.name}")
        try:
            ingest_drive_file(
                file_meta.file_id,
                download_progress_callback=None # Already downloaded
            )
            safe_print(f"[PW-{worker_id}] DONE: {file_meta.name}")
        except Exception as e:
            safe_print(f"[PW-{worker_id}] FAILED: {file_meta.name}: {e}")
        finally:
            process_q.task_done()


def main() -> None:
    parser = argparse.ArgumentParser(description="Parallel Postgres Ingestion")
    parser.add_argument("--parent", help="Parent folder ID to start from")
    parser.add_argument("--limit", type=int, default=50, help="Max files to process")
    parser.add_argument("--force", action="store_true", help="Re-process indexed files")
    args = parser.parse_args()

    app_settings = get_app_settings()
    pg_settings = get_postgres_settings()
    transcription_settings = get_transcription_settings()
    drive = GoogleDriveClient(get_google_drive_settings())
    
    # Identify the current engine_id
    current_engine_name = transcription_settings.engine
    if current_engine_name == "deepgram":
        dg_model = get_deepgram_settings().model
        engine_id = f"deepgram:{dg_model}"
    elif current_engine_name == "local":
        engine_id = f"whisper:{transcription_settings.whisper_model}"
    else:
        engine_id = current_engine_name

    safe_print(f"\n--- Folder Selection (Target: {engine_id}) ---")
    folders = list_folders(drive, args.parent)

    if not folders:
        safe_print("No folders found.")
        return

    for i, folder in enumerate(folders, 1):
        safe_print(f"[{i}] {folder.name}")

    choice = input("\nSelect folder number: ")
    if not choice or choice.lower() == 'q': return
    selected_folder = folders[int(choice) - 1]

    safe_print(f"\nScanning: {selected_folder.name}...")
    all_files = drive.list_folder_files(selected_folder.file_id, mime_prefix="video/")
    all_files, _ = dedupe_gallery_variants(all_files)
    
    files_to_process = []
    with db_connection(pg_settings) as conn:
        init_db(conn)
        for f in all_files[:args.limit]:
            existing_video = get_video_by_source_file_id(conn, source_type="google_drive", source_file_id=f.file_id)
            
            if args.force:
                files_to_process.append(f)
                continue
            
            if not existing_video:
                files_to_process.append(f)
            else:
                # Video exists, but maybe this engine transcript doesn't?
                if not check_transcript_exists(conn, existing_video["id"], engine_id):
                    files_to_process.append(f)
                elif existing_video.get("processing_status") != "indexed_chunks_ready":
                    files_to_process.append(f)

    if not files_to_process:
        safe_print("\nEverything is up to date.")
        return

    safe_print(f"\nFound {len(files_to_process)} files. Continue? (y/n): ")
    if input().lower() != 'y': return

    download_q = queue.Queue()
    process_q = queue.Queue()

    for f in files_to_process:
        download_q.put(f)

    # Workers
    for i in range(app_settings.download_concurrency):
        threading.Thread(target=download_worker, args=(drive, download_q, process_q, i+1), daemon=True).start()

    for i in range(app_settings.process_concurrency):
        threading.Thread(target=process_worker, args=(process_q, i+1), daemon=True).start()

    download_q.join()
    process_q.join()

    safe_print("\nAll files processed and indexed in Postgres!")


if __name__ == "__main__":
    main()
