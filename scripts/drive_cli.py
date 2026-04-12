from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_google_drive_settings
from app.google_drive import GoogleDriveClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Google Drive helper CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth-init", help="Generate Google OAuth URL")

    auth_exchange_parser = subparsers.add_parser(
        "auth-exchange",
        help="Exchange the Google OAuth callback URL for a stored token",
    )
    auth_exchange_parser.add_argument(
        "callback_url",
        help="Full callback URL from the browser address bar after Google login",
    )

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


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    settings = get_google_drive_settings()
    client = GoogleDriveClient(settings)

    if args.command == "auth-init":
        auth_url, session_path = client.auth_init()
        print("Open this URL in your browser:")
        print(auth_url)
        print(f"Temporary auth session saved to: {session_path}")
        return

    if args.command == "auth-exchange":
        token_payload = client.auth_exchange(args.callback_url)
        print(f"Saved token to: {settings.token_path}")
        print(
            "Refresh token present:"
            f" {'yes' if token_payload.get('refresh_token') else 'no'}"
        )
        return

    if args.command == "list":
        files = client.list_files(page_size=args.page_size)
        for item in files:
            print(
                f"id={item.file_id} name={item.name!r} "
                f"mime_type={item.mime_type!r} size={item.size!r}"
            )
        return

    output_path = (
        Path(args.output)
        if args.output
        else settings.download_dir / args.file_id
    )
    saved_path = client.download_file(args.file_id, output_path)
    print(f"Downloaded to: {saved_path}")


if __name__ == "__main__":
    main()
