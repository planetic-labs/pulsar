from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import DeepgramSettings
from app.transcription.base import TranscriptionEngine

logger = logging.getLogger(__name__)


class DeepgramEngine(TranscriptionEngine):
    def __init__(self, settings: DeepgramSettings) -> None:
        self.settings = settings

    def transcribe_file(self, audio_path: Path, **overrides) -> dict[str, Any]:
        params = {
            "model": overrides.get("model", self.settings.model),
            "language": overrides.get("language", self.settings.language),
            "smart_format": str(overrides.get("smart_format", self.settings.smart_format)).lower(),
            "punctuate": str(overrides.get("punctuate", self.settings.punctuate)).lower(),
            "utterances": str(overrides.get("utterances", self.settings.utterances)).lower(),
            "paragraphs": str(overrides.get("paragraphs", self.settings.paragraphs)).lower(),
            "diarize": str(overrides.get("diarize", self.settings.diarize)).lower(),
            "filler_words": str(overrides.get("filler_words", self.settings.filler_words)).lower(),
        }

        file_size = audio_path.stat().st_size
        logger.info(f"Starting Deepgram transcription for {audio_path.name} ({file_size / 1024 / 1024:.2f} MB)")

        headers = {
            "Authorization": f"Token {self.settings.api_key}",
            "Content-Type": "audio/wav",
        }

        start_time = time.time()
        try:
            # Use httpx with streaming to handle large files memory-efficiently
            with open(audio_path, "rb") as f:
                with httpx.Client(timeout=httpx.Timeout(600.0, connect=60.0)) as client:
                    response = client.post(
                        self.settings.base_url,
                        params=params,
                        headers=headers,
                        content=f,  # Streaming content
                    )

            duration = time.time() - start_time
            logger.info(f"Deepgram request finished in {duration:.2f}s with status {response.status_code}")

            if response.status_code != 200:
                logger.error(f"Deepgram Error ({response.status_code}): {response.text}")
                response.raise_for_status()

            return response.json()

        except httpx.TimeoutException as e:
            logger.error(f"Deepgram request timed out after {time.time() - start_time:.2f}s")
            raise RuntimeError("Deepgram transcription timed out") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"Deepgram HTTP Error: {e.response.text}")
            raise RuntimeError(f"Deepgram API returned error {e.response.status_code}") from e
        except Exception as e:
            logger.error(f"Unexpected error during Deepgram transcription: {str(e)}")
            raise

    def normalize_response(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        results = raw_payload.get("results", {})
        metadata = raw_payload.get("metadata", {})
        duration = metadata.get("duration", 0.0)

        channels = results.get("channels", [])
        if not channels:
            return {"transcript": "", "confidence": 0.0, "utterances": [], "duration": duration}

        alt = channels[0].get("alternatives", [{}])[0]

        # Deepgram returns utterances either in results or in a specific field
        utterances = results.get("utterances", [])
        if not utterances:
            # Check for paragraphs if utterances are missing
            paragraphs = alt.get("paragraphs", {}).get("paragraphs", [])
            for p in paragraphs:
                for sentence in p.get("sentences", []):
                    utterances.append(
                        {
                            "start": sentence.get("start"),
                            "end": sentence.get("end"),
                            "transcript": sentence.get("text"),
                            "speaker": p.get("speaker"),
                            "confidence": 1.0,
                        }
                    )

        normalized_utterances = []
        for utt in utterances:
            normalized_utterances.append(
                {
                    "start": float(utt.get("start", 0.0)),
                    "end": float(utt.get("end", 0.0)),
                    "text": str(utt.get("transcript", "") or utt.get("text", "")),
                    "confidence": float(utt.get("confidence", 1.0)),
                    "speaker": utt.get("speaker"),
                }
            )

        return {
            "transcript": alt.get("transcript", ""),
            "confidence": alt.get("confidence", 0.0),
            "utterances": normalized_utterances,
            "duration": duration,
        }
