from __future__ import annotations

from app.ports import ScoredPoint
from app.search.highlighter import build_quote_regex, quote_highlight, simple_highlight
from app.search.ranking import rrf_merge
from app.services.search import build_result_highlight


def test_rrf_merge():
    # Arrange
    dense: list[ScoredPoint] = [
        {"id": 1, "score": 0.9, "payload": {}},
        {"id": 2, "score": 0.8, "payload": {}},
    ]
    sparse: list[ScoredPoint] = [
        {"id": 2, "score": 0.95, "payload": {}},
        {"id": 3, "score": 0.7, "payload": {}},
    ]

    # Act
    merged = rrf_merge(dense, sparse, k=60)

    # Assert
    # Combined scores:
    # id 1: 1/(60+1) = 0.01639
    # id 2: 1/(60+2) + 1/(60+1) = 0.016129 + 0.01639 = 0.0325
    # id 3: 1/(60+2) = 0.016129
    assert len(merged) == 3
    assert merged[0]["point"]["id"] == 2
    assert merged[0]["match_type"] == "hybrid"

    assert merged[1]["point"]["id"] == 1
    assert merged[1]["match_type"] == "semantic"

    assert merged[2]["point"]["id"] == 3
    assert merged[2]["match_type"] == "keyword"


def test_simple_highlight():
    text = "Быстрый бурый лис перепрыгнул через ленивую собаку."
    query = "лис собака"

    highlighted = simple_highlight(text, query)

    assert "<mark>лис</mark>" in highlighted
    assert "<mark>собаку</mark>" in highlighted


def test_build_quote_regex():
    phrase = "быстрый лис"
    regex = build_quote_regex(phrase)

    # Words: быстрый (len 8 -> drop 3 -> быст), лис (len 3 -> лис)
    assert "быст" in regex
    assert "лис" in regex


def test_quote_highlight():
    text = "Быстрый бурый лис перепрыгнул через ленивую собаку."
    exact_phrases = ["быстрый бурый"]

    highlighted = quote_highlight(text, exact_phrases)

    assert "<mark>Быстрый</mark> <mark>бурый</mark>" in highlighted


def test_quote_highlight_does_not_mark_words_between_matches():
    text = "Быстрый совершенно посторонний бурый лис."

    highlighted = quote_highlight(text, ["быстрый бурый"])

    assert highlighted == "<mark>Быстрый</mark> совершенно посторонний <mark>бурый</mark> лис."


def test_word_search_uses_manticore_highlight():
    manticore_highlight = "Текст с <mark>найденным</mark> словом"

    highlighted = build_result_highlight("Текст с найденным словом", "найденное", "keyword", manticore_highlight)

    assert highlighted == manticore_highlight


def test_quote_search_uses_manticore_highlight():
    manticore_highlight = "<mark>Быстрый</mark> совершенно посторонний <mark>бурый</mark> лис."

    highlighted = build_result_highlight(
        "Быстрый совершенно посторонний бурый лис.",
        "быстрый бурый",
        "quote",
        manticore_highlight,
    )

    assert highlighted == manticore_highlight


def test_quote_search_uses_python_highlight_as_fallback():
    highlighted = build_result_highlight(
        "Быстрый совершенно посторонний бурый лис.",
        "быстрый бурый",
        "quote",
        None,
    )

    assert highlighted == "<mark>Быстрый</mark> совершенно посторонний <mark>бурый</mark> лис."


def test_manticore_escape_string():
    from app.manticore import escape_string

    assert escape_string("test") == "test"
    assert escape_string("O'Connor") == "O''Connor"
    assert escape_string("test\\slash") == "test\\\\slash"
    assert escape_string("test\nnew") == "test\\nnew"
    assert escape_string("test\0null") == "test\\0null"
    assert escape_string("test\x1aCtrlZ") == "test\\ZCtrlZ"
    assert escape_string("\\' OR 1=1 --") == "\\\\'' OR 1=1 --"
