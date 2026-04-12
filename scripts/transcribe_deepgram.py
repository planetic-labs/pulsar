from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_deepgram_settings
from app.transcription.deepgram import DeepgramClient, normalize_deepgram_response


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcribe a WAV file via Deepgram")
    parser.add_argument("audio_path", help="Path to WAV audio")
    parser.add_argument(
        "--output",
        help="Path to JSON output. Defaults to transcripts/<audio_stem>.deepgram.json",
    )
    parser.add_argument(
        "--normalized-output",
        help=(
            "Path to normalized JSON output. "
            "Defaults to transcripts/<audio_stem>.normalized.json"
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    audio_path = Path(args.audio_path)
    output_path = (
        Path(args.output)
        if args.output
        else ROOT_DIR / "transcripts" / f"{audio_path.stem}.deepgram.json"
    )
    normalized_path = (
        Path(args.normalized_output)
        if args.normalized_output
        else ROOT_DIR / "transcripts" / f"{audio_path.stem}.normalized.json"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.parent.mkdir(parents=True, exist_ok=True)

    client = DeepgramClient(get_deepgram_settings())
    raw_payload = client.transcribe_file(audio_path)
    normalized_payload = normalize_deepgram_response(raw_payload)

    output_path.write_text(
        json.dumps(raw_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    normalized_path.write_text(
        json.dumps(normalized_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Saved raw transcript to: {output_path}")
    print(f"Saved normalized transcript to: {normalized_path}")
    print(f"Transcript preview: {normalized_payload['transcript'][:300]}")


if __name__ == "__main__":
    main()

