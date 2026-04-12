from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.chunking import chunk_from_utterances
from app.config import get_app_settings, get_deepgram_settings, get_postgres_settings
from app.db import db_connection, init_db
from app.repository import (
    dump_summary_json,
    get_video_summary,
    replace_chunks,
    replace_transcript,
    update_video_status,
    upsert_video,
)
from app.search import build_semantic_index, rebuild_fts


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
    parser.add_argument(
        "--summary-output",
        help="Optional path for a JSON summary dump",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    normalized_payload = json.loads(
        Path(args.normalized_transcript_path).read_text(encoding="utf-8")
    )
    chunks = chunk_from_utterances(normalized_payload.get("utterances", []))

    app_settings = get_app_settings()
    deepgram_settings = get_deepgram_settings()

    with db_connection(get_postgres_settings()) as connection:
        init_db(connection)
        video_id = upsert_video(
            connection,
            source_type=args.source_type,
            source_file_id=args.source_file_id,
            title=args.title,
            source_url=args.source_url,
            mime_type=args.mime_type,
            size_bytes=args.size_bytes,
            duration_sec=args.duration_sec,
            local_video_path=args.video_path,
            local_audio_path=args.audio_path,
            processing_status="transcribed",
        )
        transcript_id = replace_transcript(
            connection,
            video_id=video_id,
            engine=f"deepgram:{deepgram_settings.model}",
            language=deepgram_settings.language,
            transcript_text=normalized_payload.get("transcript", ""),
            confidence=normalized_payload.get("confidence"),
            raw_json_path=Path(args.raw_transcript_path),
            normalized_json_path=Path(args.normalized_transcript_path),
        )
        replace_chunks(
            connection,
            video_id=video_id,
            transcript_id=transcript_id,
            chunks=chunks,
        )
        update_video_status(
            connection,
            video_id=video_id,
            processing_status="indexed_chunks_ready",
            local_audio_path=args.audio_path,
        )
        rebuild_fts(connection)
        build_semantic_index(connection)
        summary = get_video_summary(connection, video_id)

    if args.summary_output:
        summary_path = dump_summary_json(summary, Path(args.summary_output))
        print(f"Saved summary to: {summary_path}")

    print("Transcript registered and semantic index updated.")
    print(f"Video ID: {video_id}")
    print(f"Transcript ID: {transcript_id}")
    print(f"Chunks created: {len(summary['chunks'])}")
    if summary["chunks"]:
        first_chunk = summary["chunks"][0]
        print(
            "First chunk: "
            f"{first_chunk['start_sec']:.2f}-{first_chunk['end_sec']:.2f} "
            f"{first_chunk['text']}"
        )


if __name__ == "__main__":
    main()
