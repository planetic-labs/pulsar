from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _has_audio_stream(input_path: Path) -> bool:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        return "audio" in completed.stdout
    except subprocess.TimeoutExpired:
        logger.warning(f"ffprobe timed out for {input_path}")
        return False


def _get_duration(input_path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        return float(completed.stdout.strip())
    except ValueError, TypeError, subprocess.TimeoutExpired:
        return 0.0


class SilentVideoError(Exception):
    """Raised when a video has no audio stream."""

    pass


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

    logger.info(f"Начало извлечения аудио из {input_path.name}...")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if audio stream exists
    if not _has_audio_stream(input_path):
        raise SilentVideoError(f"Video file {input_path.name} has no audio stream.")

    # Use system ffmpeg directly to extract audio
    command = [
        "ffmpeg",
        "-y",
        "-nostdin",
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

    try:
        # We give it a generous timeout (30 min for very long videos)
        # but it prevents indefinite hang
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Audio extraction timed out after 30 minutes for {input_path.name}") from e

    if completed.returncode != 0:
        err_msg = (
            f"Audio extraction failed for {input_path.name}.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        raise RuntimeError(err_msg)

    logger.info(f"Аудио успешно извлечено: {output_path.name}")
    return output_path


def convert_wav_to_ogg(wav_path: Path, ogg_path: Path) -> Path:
    if not wav_path.exists():
        raise FileNotFoundError(f"Input WAV not found: {wav_path}")

    logger.info(f"Начало сжатия аудио в OGG: {wav_path.name}...")
    ogg_path.parent.mkdir(parents=True, exist_ok=True)

    # Use system ffmpeg to convert WAV to OGG using libvorbis codec
    command = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-i",
        str(wav_path),
        "-acodec",
        "libvorbis",
        str(ogg_path),
    ]

    try:
        # We give it a timeout of 10 minutes to prevent indefinite hangs
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Audio compression timed out for {wav_path.name}") from e

    if completed.returncode != 0:
        err_msg = (
            f"Audio compression failed for {wav_path.name}.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        raise RuntimeError(err_msg)

    logger.info(f"Аудио успешно сжато в OGG: {ogg_path.name}")
    return ogg_path
