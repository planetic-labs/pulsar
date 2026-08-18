from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path
from typing import Any, ClassVar

import httpx

from app.config import DeepgramSettings
from app.transcription.base import TranscriptionEngine

logger = logging.getLogger(__name__)


class DeepgramEngine(TranscriptionEngine):
    # Shared cache across all instances
    _balance_cache: ClassVar[dict[str, Any]] = {"data": None}

    def __init__(self, settings: DeepgramSettings) -> None:
        self.settings = settings

    def get_balance(self, force_refresh: bool = False) -> dict[str, Any]:
        """Returns cached balance. Network request ONLY if force_refresh is True (used by worker)."""
        if not force_refresh:
            return self._balance_cache["data"] or {"balances": []}

        # Actual network fetch
        url = f"https://api.deepgram.com/v1/projects/{self.settings.project_id}/balances"
        headers = {"Authorization": f"Token {self.settings.api_key}"}
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
                DeepgramEngine._balance_cache["data"] = data
                return data
        except Exception as e:
            logger.error(f"Failed to fetch Deepgram balance: {e!s}")
            return self._balance_cache["data"] or {"balances": []}

    async def get_balance_async(self, force_refresh: bool = False) -> dict[str, Any]:
        """Returns cached balance. Network request ONLY if force_refresh is True."""
        if not force_refresh:
            return self._balance_cache["data"] or {"balances": []}

        url = f"https://api.deepgram.com/v1/projects/{self.settings.project_id}/balances"
        headers = {"Authorization": f"Token {self.settings.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
                DeepgramEngine._balance_cache["data"] = data
                return data
        except Exception as e:
            logger.error(f"Failed to fetch Deepgram balance (async): {e!s}")
            return self._balance_cache["data"] or {"balances": []}

    def check_balance_threshold(self, threshold: float = 1.0) -> tuple[bool, float]:
        """Check if total balance is above threshold. Updates cache as side-effect."""
        # Worker calls this, forcing real network request
        data = self.get_balance(force_refresh=True)
        balances = data.get("balances", [])
        if not balances:
            return False, 0.0

        total = sum(float(b.get("amount", 0)) for b in balances)
        return total >= threshold, total

    async def check_balance_threshold_async(self, threshold: float = 1.0) -> tuple[bool, float]:
        data = await self.get_balance_async(force_refresh=True)
        balances = data.get("balances", [])
        if not balances:
            return False, 0.0
        total = sum(float(b.get("amount", 0)) for b in balances)
        return total >= threshold, total

    def transcribe_file(
        self, audio_path: Path, progress_callback: Callable[[int, int], None] | None = None, **overrides
    ) -> dict[str, Any]:
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

        content_type = "audio/ogg" if audio_path.suffix.lower() == ".ogg" else "audio/wav"
        headers = {
            "Authorization": f"Token {self.settings.api_key}",
            "Content-Type": content_type,
        }

        def file_iterator(file_path: Path, chunk_size: int = 65536) -> Generator[bytes]:
            downloaded = 0
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, file_size)
                    yield chunk

        start_time = time.time()
        try:
            # Use httpx with streaming to handle large files memory-efficiently
            with httpx.Client(timeout=httpx.Timeout(600.0, connect=60.0)) as client:
                response = client.post(
                    self.settings.base_url,
                    params=params,
                    headers=headers,
                    content=file_iterator(audio_path),
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
            logger.error(f"Unexpected error during Deepgram transcription: {e!s}")
            raise

    async def transcribe_file_async(
        self, audio_path: Path, progress_callback: Callable[[int, int], None] | None = None, **overrides
    ) -> dict[str, Any]:
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
        logger.info(f"Starting Deepgram transcription (async) for {audio_path.name} ({file_size / 1024 / 1024:.2f} MB)")

        content_type = "audio/ogg" if audio_path.suffix.lower() == ".ogg" else "audio/wav"
        headers = {
            "Authorization": f"Token {self.settings.api_key}",
            "Content-Type": content_type,
        }

        async def async_file_iterator(file_path: Path, chunk_size: int = 65536) -> AsyncGenerator[bytes]:
            uploaded = 0
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    uploaded += len(chunk)
                    if progress_callback:
                        progress_callback(uploaded, file_size)
                    yield chunk

        start_time = time.time()
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=60.0)) as client:
            response = await client.post(
                self.settings.base_url,
                params=params,
                headers=headers,
                content=async_file_iterator(audio_path),
            )

        duration = time.time() - start_time
        logger.info(f"Deepgram async request finished in {duration:.2f}s with status {response.status_code}")

        if response.status_code != 200:
            logger.error(f"Deepgram Error ({response.status_code}): {response.text}")
            response.raise_for_status()

        return response.json()

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
                    s_start = sentence.get("start")
                    s_end = sentence.get("end")
                    utterances.append(
                        {
                            "start": s_start,
                            "end": s_end,
                            "transcript": sentence.get("text") or "",
                            "confidence": 1.0,
                        }
                    )

        normalized_utterances = []
        last_end = 0.0
        for utt in utterances:
            u_start = utt.get("start")
            u_end = utt.get("end")

            # If start is missing, use last_end
            if u_start is None:
                u_start = last_end

            # If end is missing, use start + 0.1
            if u_end is None:
                u_end = float(u_start) + 0.1

            # Update last_end for next iteration
            last_end = float(u_end)

            normalized_utterances.append(
                {
                    "start": float(u_start),
                    "end": float(u_end),
                    "text": str(utt.get("transcript", "") or utt.get("text", "")),
                    "confidence": float(utt.get("confidence", 1.0)),
                }
            )

        return {
            "transcript": alt.get("transcript", ""),
            "confidence": alt.get("confidence", 0.0),
            "utterances": normalized_utterances,
            "duration": duration,
        }
