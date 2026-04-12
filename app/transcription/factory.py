from __future__ import annotations
from typing import Any
from app.config import get_transcription_settings, get_deepgram_settings
from app.transcription.base import TranscriptionEngine
from app.transcription.deepgram import DeepgramEngine
from app.transcription.whisper import LocalWhisperEngine
from app.transcription.yandex import YandexSpeechKitEngine

def get_transcription_engine() -> TranscriptionEngine:
    settings = get_transcription_settings()
    if settings.engine == "local":
        return LocalWhisperEngine(settings)
    elif settings.engine == "yandex":
        return YandexSpeechKitEngine()
    else:
        # Default to Deepgram
        return DeepgramEngine(get_deepgram_settings())
