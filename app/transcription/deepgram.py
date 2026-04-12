from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.config import DeepgramSettings
from app.transcription.base import TranscriptionEngine

class DeepgramEngine(TranscriptionEngine):
    def __init__(self, settings: DeepgramSettings) -> None:
        self.settings = settings

    def transcribe_file(self, audio_path: Path) -> dict[str, Any]:
        params = [
            f"model={self.settings.model}",
            f"language={self.settings.language}",
            f"smart_format={str(self.settings.smart_format).lower()}",
            f"punctuate={str(self.settings.punctuate).lower()}",
            f"utterances={str(self.settings.utterances).lower()}",
            f"paragraphs={str(self.settings.paragraphs).lower()}",
            f"diarize={str(self.settings.diarize).lower()}",
            f"filler_words={str(self.settings.filler_words).lower()}",
        ]
        url = f"{self.settings.base_url}?{'&'.join(params)}"
        
        request = Request(
            url,
            data=audio_path.read_bytes(),
            headers={
                "Authorization": f"Token {self.settings.api_key}",
                "Content-Type": "audio/wav",
            },
            method="POST",
        )
        with urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))

    def normalize_response(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        results = raw_payload.get("results", {})
        channels = results.get("channels", [])
        if not channels:
            return {"transcript": "", "confidence": 0.0, "utterances": []}
            
        alt = channels[0].get("alternatives", [{}])[0]
        utterances = raw_payload.get("metadata", {}).get("utterances", [])
        if not utterances:
            utterances = results.get("utterances", [])

        normalized_utterances = []
        for utt in utterances:
            normalized_utterances.append({
                "start": float(utt.get("start", 0.0)),
                "end": float(utt.get("end", 0.0)),
                "text": str(utt.get("transcript", "") or utt.get("text", "")),
                "confidence": float(utt.get("confidence", 1.0)),
            })

        return {
            "transcript": alt.get("transcript", ""),
            "confidence": alt.get("confidence", 0.0),
            "utterances": normalized_utterances,
        }
