from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_google_drive_settings
from app.config import get_app_settings
from app.db import db_connection, init_db
from app.file_dedupe import dedupe_gallery_variants
from app.google_drive import GoogleDriveClient
from app.search import build_semantic_index, rebuild_fts
from scripts.ingest_drive_file import ingest_drive_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and process the oldest files from a Google Drive folder"
    )
    parser.add_argument("folder_id", help="Google Drive folder ID")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--mime-prefix", default="video/")
    parser.add_argument(
        "--disable-gallery-dedupe",
        action="store_true",
        help="Do not collapse Gallery/Галерея duplicate Zoom variants",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue processing the remaining files if one file fails",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    drive = GoogleDriveClient(get_google_drive_settings())
    files = drive.list_folder_files(
        args.folder_id,
        order_by="createdTime asc",
        mime_prefix=args.mime_prefix,
    )
    if not args.disable_gallery_dedupe:
        files, skipped_pairs = dedupe_gallery_variants(files)
        if skipped_pairs:
            print("Skipped Gallery duplicates:")
            for skipped, preferred in skipped_pairs:
                print(f"- skip {skipped.name} -> keep {preferred.name}")
    files = files[: args.limit]
    if not files:
        raise RuntimeError("No matching files found in the folder.")

    print(f"Selected {len(files)} file(s) from folder {args.folder_id}")
    for index, file_meta in enumerate(files, start=1):
        print(
            f"[{index}/{len(files)}] {file_meta.file_id} "
            f"{file_meta.created_time or '-'} {file_meta.name}"
        )

    for index, file_meta in enumerate(files, start=1):
        print(f"Processing [{index}/{len(files)}]: {file_meta.name}")
        try:
            ingest_drive_file(file_meta.file_id, rebuild_search_index=False)
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED [{index}/{len(files)}]: {file_meta.name}: {exc}")
            if not args.continue_on_error:
                raise
        else:
            print(f"DONE [{index}/{len(files)}]: {file_meta.name}")

    app_settings = get_app_settings()
    with db_connection(app_settings) as connection:
        init_db(connection)
        rebuild_fts(connection)
        try:
            build_semantic_index(connection, app_settings.semantic_index_path)
        except Exception as exc:  # noqa: BLE001
            print(f"Semantic index rebuild skipped due to error: {exc}")
        else:
            print("Rebuilt shared search index after batch processing.")


if __name__ == "__main__":
    main()
