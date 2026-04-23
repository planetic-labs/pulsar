from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.audio import extract_audio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract WAV audio from a video file")
    parser.add_argument("input_path", help="Path to the source video file")
    parser.add_argument(
        "--output",
        help="Path to the output WAV file. Defaults to audio/<video_stem>.wav",
    )
    parser.add_argument("--sample-rate", type=int, default=16_000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--start-sec", type=float)
    parser.add_argument("--duration-sec", type=float)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    input_path = Path(args.input_path)
    output_path = Path(args.output) if args.output else ROOT_DIR / "audio" / f"{input_path.stem}.wav"

    extracted = extract_audio(
        input_path,
        output_path,
        sample_rate=args.sample_rate,
        channels=args.channels,
        start_sec=args.start_sec,
        duration_sec=args.duration_sec,
    )
    print(f"Audio extracted to: {extracted}")


if __name__ == "__main__":
    main()
