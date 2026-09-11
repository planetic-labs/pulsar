from __future__ import annotations

import gzip
import json
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from app.search.highlighter import build_quote_regex

WordTiming = tuple[str, float, float]


class QuoteTimingResolver:
    """Resolve quote starts from Deepgram word timings with a small file-aware LRU cache."""

    def __init__(self, maxsize: int = 8) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self._cache: OrderedDict[Path, tuple[int, int, tuple[WordTiming, ...]]] = OrderedDict()
        self._lock = threading.Lock()

    def resolve(self, path: Path, query: str, chunk_start: float, chunk_end: float) -> tuple[float, bool]:
        """Return the first matching word start, or the chunk start when it cannot be resolved."""
        clean_query = re.sub(r"(?:video_id|v):[^\s]+", "", query).strip()
        pattern = build_quote_regex(clean_query)
        if not pattern:
            return chunk_start, False

        try:
            words = self._load_words(path)
        except OSError, ValueError, TypeError, json.JSONDecodeError:
            return chunk_start, False

        chunk_words = [word for word in words if word[1] < chunk_end and word[2] > chunk_start]
        if not chunk_words:
            return chunk_start, False

        transcript = " ".join(word[0] for word in chunk_words)
        try:
            match = re.search(pattern, transcript, flags=re.IGNORECASE | re.UNICODE)
        except re.error:
            return chunk_start, False
        if match is None:
            return chunk_start, False

        word_index = transcript[: match.start()].count(" ")
        return max(chunk_start, chunk_words[word_index][1]), True

    def _load_words(self, path: Path) -> tuple[WordTiming, ...]:
        stat = path.stat()
        with self._lock:
            cached = self._cache.get(path)
            if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                self._cache.move_to_end(path)
                return cached[2]

        with gzip.open(path, "rt", encoding="utf-8") as file:
            payload = json.load(file)
        words = self._extract_words(payload)

        with self._lock:
            self._cache[path] = (stat.st_mtime_ns, stat.st_size, words)
            self._cache.move_to_end(path)
            while len(self._cache) > self.maxsize:
                self._cache.popitem(last=False)
        return words

    @staticmethod
    def _extract_words(payload: dict[str, Any]) -> tuple[WordTiming, ...]:
        channels = payload.get("results", {}).get("channels", [])
        alternatives = channels[0].get("alternatives", []) if channels else []
        raw_words = alternatives[0].get("words", []) if alternatives else []
        words: list[WordTiming] = []
        for item in raw_words:
            text = str(item.get("word") or item.get("punctuated_word") or "").strip()
            if not text:
                continue
            try:
                start = float(item["start"])
                end = float(item.get("end", start))
            except KeyError, TypeError, ValueError:
                continue
            words.append((text, start, end))
        return tuple(words)


quote_timing_resolver = QuoteTimingResolver()
