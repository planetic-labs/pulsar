import os
import torch
import torchaudio
import logging
from speechbrain.inference.speaker import EncoderClassifier
from pathlib import Path

logger = logging.getLogger(__name__)

# Singleton for the voice model
_encoder = None

def get_voice_encoder():
    global _encoder
    if _encoder is None:
        # Note: In a real app we might want to broadcast this via WebSocket, 
        # but for now we'll just log it clearly.
        logger.info("Initializing Speaker Recognition model (ECAPA-TDNN). This may take a minute on first run...")
        cache_dir = Path("/srv/search-ui/models_cache/voice")
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        _encoder = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=cache_dir,
            run_opts={"device": "cpu"} # Force CPU for stability on servers
        )
    return _encoder

def extract_speaker_embedding(audio_path: Path, start_sec: float, end_sec: float) -> list[float]:
    """Extracts a voice fingerprint from a specific segment of audio."""
    encoder = get_voice_encoder()
    
    try:
        # 1. Load the specific segment (using torchaudio for efficiency)
        info = torchaudio.info(str(audio_path))
        sample_rate = info.sample_rate
        
        frame_offset = int(start_sec * sample_rate)
        num_frames = int((end_sec - start_sec) * sample_rate)
        
        waveform, sr = torchaudio.load(str(audio_path), frame_offset=frame_offset, num_frames=num_frames)
        
        # 2. Convert to mono if needed
        if waveform.shape[0] > 1:
            waveform = torch.mean(waveform, dim=0, keepdim=True)
            
        # 3. Generate embedding
        with torch.no_grad():
            embeddings = encoder.encode_batch(waveform)
            # The output is [batch, 1, 192], we need [192]
            vector = embeddings.flatten().tolist()
            
        return vector
    except Exception as e:
        logger.error(f"Failed to extract voice embedding: {e}")
        return []
