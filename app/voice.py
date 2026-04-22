import os
import torch
import torchaudio
import logging
import subprocess
import tempfile
import imageio_ffmpeg
import soundfile as sf
import gc
from speechbrain.inference.speaker import EncoderClassifier
from pathlib import Path

logger = logging.getLogger(__name__)

# Singleton for the voice model
_encoder = None

def get_voice_encoder():
    global _encoder
    if _encoder is None:
        # Crucial: clean up memory before loading heavy model
        logger.info("Cleaning up memory before loading voice model...")
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            
        logger.info("Initializing Speaker Recognition model (ECAPA-TDNN)...")
        cache_dir = Path("/srv/search-ui/models_cache/voice")
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        _encoder = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=cache_dir,
            run_opts={"device": "cpu"}
        )
        logger.info("Voice model loaded successfully.")
    return _encoder

def extract_speaker_embedding(audio_path: Path, start_sec: float, end_sec: float) -> list[float]:
    """Extracts a voice fingerprint from a specific segment of audio."""
    encoder = get_voice_encoder()
    
    # We use ffmpeg to extract and resample to ensure it is in the 16kHz mono format SpeechBrain prefers.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
        
    try:
        duration = end_sec - start_sec
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        # Extract segment: -ss (start), -t (duration), -ar (16kHz), -ac (mono)
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
        
        # Load with soundfile instead of torchaudio
        audio_data, samplerate = sf.read(tmp_path)
        # Convert to torch tensor: [batch, frames]
        waveform = torch.from_numpy(audio_data).float().unsqueeze(0)
            
        # Generate embedding
        with torch.no_grad():
            embeddings = encoder.encode_batch(waveform)
            # The output is [batch, 1, 192], we need [192]
            vector = embeddings.flatten().tolist()
            
        return vector
    except Exception as e:
        logger.error(f"Failed to extract voice embedding: {e}")
        return []
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
