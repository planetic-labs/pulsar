from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.chunking import chunk_from_utterances
from app.config import get_app_settings, get_sqlite_settings
from app.db import db_connection, init_db
from app.repository import (
    replace_chunks,
    update_video_status,
    upsert_video,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register local video/audio/transcript artifacts into the MVP database"
    )
    parser.add_argument("--source-type", default="google_drive")
    parser.add_argument("--source-file-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-url")
    parser.add_argument("--mime-type", default="video/mp4")
    parser.add_argument("--size-bytes", type=int)
    parser.add_argument("--duration-sec", type=float)
    parser.add_argument("--video-path", required=True)
    parser.add_argument("--audio-path", required=True)
    parser.add_argument("--raw-transcript-path", required=True)
    parser.add_argument("--normalized-transcript-path", required=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    normalized_payload = json.loads(Path(args.normalized_transcript_path).read_text(encoding="utf-8"))
    chunks = chunk_from_utterances(normalized_payload.get("utterances", []))

    app_settings = get_app_settings()

    # Copy files to designated storage directories
    dest_raw = app_settings.raw_transcripts_dir / f"{args.source_file_id}.json"
    dest_norm = app_settings.normalized_transcripts_dir / f"{args.source_file_id}.json"
    dest_raw.parent.mkdir(parents=True, exist_ok=True)
    dest_norm.parent.mkdir(parents=True, exist_ok=True)

    import shutil

    shutil.copy2(args.raw_transcript_path, dest_raw)
    shutil.copy2(args.normalized_transcript_path, dest_norm)

    with db_connection(get_sqlite_settings()) as connection:
        init_db(connection)
        video_id = upsert_video(
            connection,
            source_file_id=args.source_file_id,
            title=args.title,
            source_url=args.source_url,
            mime_type=args.mime_type,
            size_bytes=args.size_bytes,
            duration_sec=args.duration_sec,
            status="transcribed",
        )
        replace_chunks(
            connection,
            video_id=video_id,
            chunks=chunks,
        )
        update_video_status(
            connection,
            video_id=video_id,
            status="indexed_chunks_ready",
        )

    print("Transcript registered.")
    print(f"Video ID: {video_id}")


if __name__ == "__main__":
    main()
