from __future__ import annotations
from pathlib import Path
from typing import Any
from app.transcription.base import TranscriptionEngine

class YandexSpeechKitEngine(TranscriptionEngine):
    def transcribe_file(self, audio_path: Path) -> dict[str, Any]:
        # TODO: Implement Yandex SpeechKit API
        return {
            "error": "Yandex SpeechKit is not implemented yet",
            "transcript": "Yandex Stub",
            "utterances": []
        }

    def normalize_response(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "transcript": "Yandex Stub",
            "confidence": 0.0,
            "utterances": [],
        }
