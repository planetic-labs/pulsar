from __future__ import annotations

import gzip
import json
from pathlib import Path

from app.services.quote_timing import QuoteTimingResolver


def write_transcript(path: Path, words: list[dict[str, str | float]]) -> None:
    payload = {"results": {"channels": [{"alternatives": [{"words": words}]}]}}
    with gzip.open(path, "wt", encoding="utf-8") as file:
        json.dump(payload, file)


def test_resolves_first_word_of_quote(tmp_path: Path):
    path = tmp_path / "transcript.json.gz"
    write_transcript(
        path,
        [
            {"word": "до", "start": 10.0, "end": 10.2},
            {"word": "первого", "start": 10.3, "end": 10.7},
            {"word": "совпадения", "start": 10.8, "end": 11.2},
        ],
    )

    start, matched = QuoteTimingResolver().resolve(path, "первое совпадение", 9.0, 12.0)

    assert matched is True
    assert start == 10.3


def test_falls_back_to_chunk_start_when_quote_is_missing(tmp_path: Path):
    path = tmp_path / "transcript.json.gz"
    write_transcript(path, [{"word": "другой", "start": 4.0, "end": 4.5}])

    start, matched = QuoteTimingResolver().resolve(path, "нет совпадения", 3.0, 5.0)

    assert matched is False
    assert start == 3.0


def test_lru_cache_is_bounded(tmp_path: Path):
    resolver = QuoteTimingResolver(maxsize=1)
    first = tmp_path / "first.json.gz"
    second = tmp_path / "second.json.gz"
    write_transcript(first, [{"word": "первый", "start": 1.0, "end": 1.5}])
    write_transcript(second, [{"word": "второй", "start": 2.0, "end": 2.5}])

    resolver.resolve(first, "первый", 0.0, 3.0)
    resolver.resolve(second, "второй", 0.0, 3.0)

    assert list(resolver._cache) == [second]
