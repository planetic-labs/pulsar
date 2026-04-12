from __future__ import annotations

from pathlib import Path
import subprocess

import imageio_ffmpeg


def extract_audio(
    input_path: Path,
    output_path: Path,
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
    start_sec: float | None = None,
    duration_sec: float | None = None,
) -> Path:
    if not input_path.exists():
        raise FileNotFoundError(f"Input video not found: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg_path,
        "-y",
    ]
    if start_sec is not None:
        command.extend(["-ss", str(start_sec)])
    command.extend(["-i", str(input_path)])
    if duration_sec is not None:
        command.extend(["-t", str(duration_sec)])
    command.extend(
        [
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            str(output_path),
        ]
    )

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Audio extraction failed.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )

    return output_path

