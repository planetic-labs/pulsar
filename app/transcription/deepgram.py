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

    def transcribe_file(self, audio_path: Path, **overrides) -> dict[str, Any]:
        params = [
            f"model={overrides.get('model', self.settings.model)}",
            f"language={overrides.get('language', self.settings.language)}",
            f"smart_format={str(overrides.get('smart_format', self.settings.smart_format)).lower()}",
            f"punctuate={str(overrides.get('punctuate', self.settings.punctuate)).lower()}",
            f"utterances={str(overrides.get('utterances', self.settings.utterances)).lower()}",
            f"paragraphs={str(overrides.get('paragraphs', self.settings.paragraphs)).lower()}",
            f"diarize={str(overrides.get('diarize', self.settings.diarize)).lower()}",
            f"filler_words={str(overrides.get('filler_words', self.settings.filler_words)).lower()}",
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
        
        # Deepgram returns utterances either in results or in a specific field
        utterances = results.get("utterances", [])
        if not utterances:
            # Check for paragraphs if utterances are missing
            paragraphs = alt.get("paragraphs", {}).get("paragraphs", [])
            for p in paragraphs:
                for sentence in p.get("sentences", []):
                    utterances.append({
                        "start": sentence.get("start"),
                        "end": sentence.get("end"),
                        "transcript": sentence.get("text"),
                        "speaker": p.get("speaker"),
                        "confidence": 1.0
                    })

        normalized_utterances = []
        for utt in utterances:
            normalized_utterances.append({
                "start": float(utt.get("start", 0.0)),
                "end": float(utt.get("end", 0.0)),
                "text": str(utt.get("transcript", "") or utt.get("text", "")),
                "confidence": float(utt.get("confidence", 1.0)),
                "speaker": utt.get("speaker")
            })

        return {
            "transcript": alt.get("transcript", ""),
            "confidence": alt.get("confidence", 0.0),
            "utterances": normalized_utterances,
        }
