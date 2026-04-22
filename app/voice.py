import os
import logging
import subprocess
import tempfile
import imageio_ffmpeg
import httpx
import numpy as np
from pathlib import Path
from app.config import get_voice_settings

logger = logging.getLogger(__name__)

def l2_normalize(vector: list[float]) -> list[float]:
    arr = np.array(vector)
    norm = np.linalg.norm(arr)
    if norm == 0: return vector
    return (arr / norm).tolist()

def extract_speaker_embedding(audio_path: Path, start_sec: float, end_sec: float) -> list[float]:
    """Extracts a voice fingerprint using Custom FastAPI Voice API."""
    settings = get_voice_settings()
    if not settings.voice_api_token or not settings.voice_api_url:
        logger.warning("VOICE_API_TOKEN or VOICE_API_URL not set, skipping voice embedding extraction.")
        return []

    url = settings.voice_api_url
    if not url.endswith("/embed"):
        url = url.rstrip("/") + "/embed"

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
        
    try:
        duration = end_sec - start_sec
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_path, "-y", "-loglevel", "error",
            "-ss", str(start_sec),
            "-i", str(audio_path),
            "-t", str(duration),
            "-ar", "16000",
            "-ac", "1",
            tmp_path
        ]
        subprocess.run(cmd, check=True)
        
        headers = {
            "Authorization": f"Bearer {settings.voice_api_token}"
        }
        
        with open(tmp_path, "rb") as f:
            files = {"file": (f"segment_{int(start_sec)}.wav", f, "audio/wav")}
            
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    url,
                    headers=headers,
                    files=files
                )
                response.raise_for_status()
                result = response.json()
            
            vector = []
            if isinstance(result, list):
                vector = result
            elif isinstance(result, dict) and "embedding" in result:
                vector = result["embedding"]
            
            if vector:
                # Ensure it is L2-normalized for consistent Cosine search
                return l2_normalize(vector)
            
            logger.error(f"Unexpected response format from Voice API: {result}")
            return []

    except Exception as e:
        logger.error(f"Failed to extract voice embedding via API: {e}")
        return []
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
