from __future__ import annotations

import json
import time
import logging
import httpx
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from typing import List, Dict, Any, Optional, Tuple
from app.config import GeminiSettings, EmbeddingSettings
from qdrant_client import models

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

class UnifiedEmbeddingClient:
    def __init__(self, gemini_settings: GeminiSettings, hf_settings: EmbeddingSettings) -> None:
        self.gemini_settings = gemini_settings
        self.hf_settings = hf_settings
        
        # Check if we should use HF Remote Service
        self.use_hf = bool(hf_settings.api_url)
        
        if self.use_hf:
            logger.info(f"Using Remote Embedding Service at {hf_settings.api_url}")
        else:
            logger.info("Remote Embedding Service not configured, falling back to Gemini API.")
            model_name = self.gemini_settings.embedding_model
            if not model_name.startswith("models/"):
                model_name = f"models/{model_name}"
            self.api_url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:"
            
            rpm = gemini_settings.embedding_rpm_limit or 90
            tpm = 25000 
            self.monitors = [KeyMonitor(k, rpm, tpm) for k in gemini_settings.api_keys]

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // 2 + 10

    def _get_available_monitor(self, estimated_tokens: int) -> KeyMonitor:
        while True:
            for monitor in self.monitors:
                if monitor.is_available(estimated_tokens):
                    return monitor
            logger.info("All API keys are hitting rate limits. Waiting 5s...")
            time.sleep(5)

    def _do_gemini_request(self, method: str, payload: dict, estimated_tokens: int) -> dict:
        monitor = self._get_available_monitor(estimated_tokens)
        url = self.api_url + method + f"?key={monitor.key}"
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(self.gemini_settings.max_retries):
            try:
                with urlopen(request, timeout=120) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 429:
                    monitor.set_cooldown(70)
                    return self._do_gemini_request(method, payload, estimated_tokens)
                raise exc
            except Exception as e:
                raise e
        raise RuntimeError("Gemini request failed.")

    def embed_text(self, text: str, task_type: str = "RETRIEVAL_QUERY") -> Tuple[List[float], Optional[models.SparseVector]]:
        """Returns (dense_vector, sparse_vector) for a single text."""
        if self.use_hf:
            url = f"{self.hf_settings.api_url.rstrip('/')}/embeddings"
            headers = {"Authorization": f"Bearer {self.hf_settings.api_token}"} if self.hf_settings.api_token else {}
            payload = {"model": self.hf_settings.model_id, "input": [text]}
            
            try:
                with httpx.Client(timeout=60.0) as client:
                    response = client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    res = response.json()
                    
                dense = res["data"][0]["embedding"]
                sparse = None
                # Check for sparse in response (Infinity specific)
                if "usage" in res and "embeddings_sparse" in res["data"][0]:
                    sparse_data = res["data"][0]["embeddings_sparse"]
                    sparse = models.SparseVector(
                        indices=sparse_data["indices"],
                        values=sparse_data["values"]
                    )
                return dense, sparse
            except Exception as e:
                logger.error(f"Remote embedding failed: {e}")
                raise e
        else:
            # Fallback to Gemini (Dense only)
            tokens = self._estimate_tokens(text)
            payload = {
                "model": f"models/{self.gemini_settings.embedding_model}" if not self.gemini_settings.embedding_model.startswith("models/") else self.gemini_settings.embedding_model,
                "content": {"parts": [{"text": text}]},
                "taskType": task_type,
                "outputDimensionality": self.gemini_settings.embedding_dimension,
            }
            res = self._do_gemini_request("embedContent", payload, tokens)
            return res["embedding"]["values"], None

    def embed_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[Tuple[List[float], Optional[models.SparseVector]]]:
        if not texts: return []
        
        if self.use_hf:
            # Remote batch processing
            url = f"{self.hf_settings.api_url.rstrip('/')}/embeddings"
            headers = {"Authorization": f"Bearer {self.hf_settings.api_token}"} if self.hf_settings.api_token else {}
            
            results = []
            # Infinity handles large batches well
            for i in range(0, len(texts), 50):
                batch = texts[i : i + 50]
                payload = {"model": self.hf_settings.model_id, "input": batch}
                try:
                    with httpx.Client(timeout=120.0) as client:
                        response = client.post(url, json=payload, headers=headers)
                        response.raise_for_status()
                        res = response.json()
                    
                    for item in res["data"]:
                        dense = item["embedding"]
                        sparse = None
                        if "embeddings_sparse" in item:
                            s = item["embeddings_sparse"]
                            sparse = models.SparseVector(indices=s["indices"], values=s["values"])
                        results.append((dense, sparse))
                except Exception as e:
                    logger.error(f"Remote batch embedding failed: {e}")
                    raise e
            return results
        else:
            # Gemini batch (Dense only)
            i = 0
            results = []
            while i < len(texts):
                current_batch = []
                batch_tokens = 0
                for j in range(i, min(i + 100, len(texts))):
                    item_tokens = self._estimate_tokens(texts[j])
                    if batch_tokens + item_tokens > 12000 and current_batch: break
                    current_batch.append(texts[j])
                    batch_tokens += item_tokens
                
                batch_size = len(current_batch)
                model_path = f"models/{self.gemini_settings.embedding_model}" if not self.gemini_settings.embedding_model.startswith("models/") else self.gemini_settings.embedding_model
                requests = [
                    {
                        "model": model_path,
                        "content": {"parts": [{"text": t}]},
                        "taskType": task_type,
                        "outputDimensionality": self.gemini_settings.embedding_dimension,
                    }
                    for t in current_batch
                ]
                payload = {"requests": requests}
                res = self._do_gemini_request("batchEmbedContents", payload, batch_tokens)
                for e in res["embeddings"]:
                    results.append((e["values"], None))
                i += batch_size
            return results

# For backward compatibility
GeminiEmbeddingClient = UnifiedEmbeddingClient
