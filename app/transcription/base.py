from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class TranscriptionEngine(ABC):
    @abstractmethod
    def transcribe_file(self, audio_path: Path) -> dict[str, Any]:
        """Transcribe an audio file and return a raw response."""
        pass

    @abstractmethod
    def normalize_response(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize the raw response to a standard format."""
        pass
