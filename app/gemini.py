from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from app.config import GeminiSettings


class GeminiEmbeddingClient:
    def __init__(self, settings: GeminiSettings) -> None:
        self.settings = settings
        self._last_request_ts = 0.0
        # Keep a safety margin below hard RPM cap.
        effective_rpm = max(1, settings.embedding_rpm_limit)
        self._min_interval_sec = 60.0 / float(effective_rpm)

    def _wait_for_rate_limit(self) -> None:
        now = time.time()
        delta = now - self._last_request_ts
        if delta < self._min_interval_sec:
            time.sleep(self._min_interval_sec - delta)

    def _sleep_after_429(self, exc: HTTPError, details: str, attempt: int) -> None:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                wait_sec = float(retry_after)
            except ValueError:
                wait_sec = 0.0
        else:
            match = re.search(r"retry in\s+([0-9.]+)s", details, re.IGNORECASE)
            wait_sec = float(match.group(1)) if match else 0.0

        if wait_sec <= 0:
            # Conservative fallback backoff.
            wait_sec = min(60.0, 2.0 ** min(attempt, 6))

        # Add a little jitter buffer so we don't hit the exact same wall again.
        time.sleep(wait_sec + 0.5)

    def embed_text(self, text: str, *, task_type: str) -> np.ndarray:
        payload = {
            "model": f"models/{self.settings.embedding_model}",
            "content": {
                "parts": [
                    {
                        "text": text,
                    }
                ]
            },
            "taskType": task_type,
            "outputDimensionality": self.settings.embedding_dimension,
        }
        url = (
            f"{self.settings.base_url}/models/"
            f"{self.settings.embedding_model}:embedContent"
        )
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.settings.api_key,
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries):
            self._wait_for_rate_limit()
            try:
                with urlopen(request, timeout=120) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self._last_request_ts = time.time()
                break
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(
                    f"Gemini embedding request failed with HTTP {exc.code}: {details}"
                )
                if exc.code == 429 and attempt < self.settings.max_retries - 1:
                    self._sleep_after_429(exc, details, attempt)
                    continue
                raise last_error from exc
        else:
            raise RuntimeError("Gemini embedding request failed after retries.") from last_error

        embedding = body.get("embedding") or {}
        values = embedding.get("values") or []
        if not values:
            raise RuntimeError(f"Gemini embedding response missing values: {body}")

        vector = np.asarray(values, dtype=np.float32)
        norm = np.linalg.norm(vector)
        if norm == 0:
            return vector
        return vector / norm
