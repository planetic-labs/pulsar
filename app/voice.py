import logging
import os
from pathlib import Path

import httpx
import numpy as np

from app.config import get_voice_settings

logger = logging.getLogger(__name__)


def normalize_vector(vector: list[float]) -> list[float]:
    """L2 normalization for cosine similarity compatibility."""
    if not vector:
        return []
    arr = np.array(vector)
    norm = np.linalg.norm(arr)
    if norm == 0:
        return vector
    return (arr / norm).tolist()


def extract_speaker_embedding(audio_path: Path, start_sec: float, end_sec: float) -> list[float] | None:
    """
    Calls external Voice API (SpeechBrain ECAPA-TDNN) to get a 192d embedding.
    """
    settings = get_voice_settings()
    if not settings.voice_api_url:
        logger.warning("VOICE_API_URL not set, skipping speaker embedding.")
        return None

    # We use a temporary file for the segment to send to the API
    import subprocess

    import imageio_ffmpeg

    temp_segment = Path(f"temp_seg_{os.getpid()}.wav")
    try:
        duration = end_sec - start_sec
        # Extract segment using ffmpeg
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_path,
            "-y",
            "-loglevel",
            "error",
            "-ss",
            str(start_sec),
            "-i",
            str(audio_path),
            "-t",
            str(duration),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(temp_segment),
        ]
        subprocess.run(cmd, check=True)

        # Send to Voice API
        with open(temp_segment, "rb") as f:
            files = {"file": (temp_segment.name, f, "audio/wav")}
            headers = {}
            if settings.voice_api_token:
                headers["Authorization"] = f"Bearer {settings.voice_api_token}"

            with httpx.Client(timeout=30.0) as client:
                response = client.post(settings.voice_api_url, files=files, headers=headers)
                response.raise_for_status()
                result = response.json()
                embedding = result.get("embedding")

                if embedding:
                    # Voice API should already normalize, but we ensure it
                    result_vec = normalize_vector(embedding)
                    return [float(x) for x in result_vec]

    except Exception as e:
        logger.error(f"Failed to extract speaker embedding: {e}")
    finally:
        if temp_segment.exists():
            temp_segment.unlink()

    return None
