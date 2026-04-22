from __future__ import annotations

import json
import time
import logging
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from typing import List, Dict
from app.config import GeminiSettings

logger = logging.getLogger(__name__)

class KeyMonitor:
    """Monitor for a single API Key limits."""
    def __init__(self, key: str, rpm_limit: int, tpm_limit: int):
        self.key = key
        self.rpm_limit = rpm_limit
        self.tpm_limit = tpm_limit
        self.tokens_in_window = 0
        self.requests_in_window = 0
        self.window_start = time.time()
        self.cooldown_until = 0.0

    def is_available(self, estimated_tokens: int) -> bool:
        now = time.time()
        if now < self.cooldown_until:
            return False
            
        elapsed = now - self.window_start
        if elapsed > 60:
            self.tokens_in_window = 0
            self.requests_in_window = 0
            self.window_start = now
            return True
            
        if self.requests_in_window >= self.rpm_limit:
            return False
        if self.tokens_in_window + estimated_tokens >= self.tpm_limit:
            return False
            
        return True

    def record_usage(self, tokens: int):
        self.requests_in_window += 1
        self.tokens_in_window += tokens

    def set_cooldown(self, seconds: int = 65):
        self.cooldown_until = time.time() + seconds
        logger.warning(f"Key {self.key[:8]}... put on cooldown for {seconds}s")

class GeminiEmbeddingClient:
    def __init__(self, settings: GeminiSettings) -> None:
        self.settings = settings
        model_name = self.settings.embedding_model
        if not model_name.startswith("models/"):
            model_name = f"models/{model_name}"
        self.full_model_name = model_name
        self.api_url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:"
        
        # Initialize monitors for each key
        rpm = settings.embedding_rpm_limit or 90
        tpm = 25000 # Safety buffer for 30k Free Tier
        
        self.monitors = [KeyMonitor(k, rpm, tpm) for k in settings.api_keys]
        if not self.monitors:
            raise ValueError("No Google AI API keys provided.")

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // 2 + 10

    def _get_available_monitor(self, estimated_tokens: int) -> KeyMonitor:
        """Rotates through keys to find one that is ready."""
        while True:
            # First pass: try to find an instantly available key
            for monitor in self.monitors:
                if monitor.is_available(estimated_tokens):
                    return monitor
            
            # Second pass: if no key is available, sleep and retry
            logger.info("All API keys are hitting rate limits. Waiting 5s...")
            time.sleep(5)

    def _do_request(self, method: str, payload: dict, estimated_tokens: int) -> dict:
        monitor = self._get_available_monitor(estimated_tokens)
        
        url = self.api_url + method + f"?key={monitor.key}"
        
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        
        for attempt in range(self.settings.max_retries):
            try:
                with urlopen(request, timeout=120) as response:
                    body = json.loads(response.read().decode("utf-8"))
                
                monitor.record_usage(estimated_tokens)
                return body
            
            except HTTPError as exc:
                if exc.code == 429:
                    monitor.set_cooldown(70)
                    # Try another key immediately
                    return self._do_request(method, payload, estimated_tokens)
                logger.error(f"Gemini API Error {exc.code} for {url}: {exc.read().decode('utf-8')}")
                raise exc
            except Exception as e:
                logger.error(f"Unexpected Gemini API error for {url}: {e}")
                raise e
        
        raise RuntimeError("Gemini request failed after retries.")

    def embed_text(self, text: str, task_type: str = "RETRIEVAL_QUERY") -> List[float]:
        """Embed a single string (useful for queries)."""
        tokens = self._estimate_tokens(text)
        payload = {
            "model": self.full_model_name,
            "content": {"parts": [{"text": text}]},
            "taskType": task_type,
            "outputDimensionality": self.settings.embedding_dimension,
        }
        res = self._do_request("embedContent", payload, tokens)
        return res["embedding"]["values"]

    def embed_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        if not texts: return []
        
        results = []
        i = 0
        while i < len(texts):
            current_batch = []
            batch_tokens = 0
            
            # Form a batch (up to 100 items OR 12k tokens per batch for safety)
            for j in range(i, min(i + 100, len(texts))):
                item_tokens = self._estimate_tokens(texts[j])
                if batch_tokens + item_tokens > 12000 and current_batch:
                    break
                current_batch.append(texts[j])
                batch_tokens += item_tokens
            
            batch_size = len(current_batch)
            logger.info(f"Processing batch of {batch_size} chunks via key rotation...")
            
            requests = [
                {
                    "model": self.full_model_name,
                    "content": {"parts": [{"text": t}]},
                    "taskType": task_type,
                    "outputDimensionality": self.settings.embedding_dimension,
                }
                for t in current_batch
            ]
            
            payload = {"requests": requests}
            res = self._do_request("batchEmbedContents", payload, batch_tokens)
            results.extend([e["values"] for e in res["embeddings"]])
            
            i += batch_size
            time.sleep(0.1) # Minimum spacing between requests
            
        return results
