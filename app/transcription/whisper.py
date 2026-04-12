from __future__ import annotations
from pathlib import Path
from typing import Any
from app.config import TranscriptionSettings
from app.transcription.base import TranscriptionEngine

class LocalWhisperEngine(TranscriptionEngine):
    def __init__(self, settings: TranscriptionSettings) -> None:
        self.settings = settings
        self.model = None # Lazy load

    def transcribe_file(self, audio_path: Path) -> dict[str, Any]:
        # Note: This requires faster-whisper package
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise ImportError("faster-whisper is not installed. Run: pip install faster-whisper")
        
        if self.model is None:
            # CPU optimization: use int8 for efficiency without sacrificing much quality
            compute_type = "int8" if self.settings.whisper_device == "cpu" else "float16"
            self.model = WhisperModel(
                self.settings.whisper_model, 
                device=self.settings.whisper_device,
                compute_type=compute_type
            )
        
        segments, info = self.model.transcribe(str(audio_path), beam_size=5)
        
        results = []
        full_text = []
        for segment in segments:
            results.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
                "confidence": 1.0 # Whisper doesn't give direct confidence per segment easily
            })
            full_text.append(segment.text.strip())
            
        return {
            "transcript": " ".join(full_text),
            "utterances": results,
            "language": info.language
        }

    def normalize_response(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "transcript": raw_payload.get("transcript", ""),
            "confidence": 1.0,
            "utterances": raw_payload.get("utterances", []),
        }
