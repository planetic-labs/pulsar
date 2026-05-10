from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_google_drive_settings
from app.google_drive import GoogleDriveClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Google Drive helper CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List files from Google Drive")
    list_parser.add_argument("--page-size", type=int, default=10)

    download_parser = subparsers.add_parser(
        "download",
        help="Download a single file from Google Drive",
    )
    download_parser.add_argument("file_id", help="Google Drive file ID")
    download_parser.add_argument(
        "--output",
        help="Destination path. Defaults to downloads/<file_id>",
    )

    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    settings = get_google_drive_settings()
    client = GoogleDriveClient(settings)

    # Print service account email for convenience
    try:
        creds_info = json.loads(settings.credentials_path.read_text())
        print(f"Using Service Account: {creds_info.get('client_email')}")
    except Exception:
        pass

    if args.command == "list":
        files = await client.list_files(page_size=args.page_size)
        for item in files:
            parents_str = ",".join(item.parents) if item.parents else "None"
            print(f"id={item.file_id} name={item.name!r} mime_type={item.mime_type!r} parents=[{parents_str}] size={item.size!r}")
        return

    output_path = Path(args.output) if args.output else settings.download_dir / args.file_id
    saved_path = await client.download_file(args.file_id, output_path)
    print(f"Downloaded to: {saved_path}")


if __name__ == "__main__":
    asyncio.run(main())
