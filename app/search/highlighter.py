from __future__ import annotations

import re

import pymorphy3

_morph = pymorphy3.MorphAnalyzer()


def simple_highlight(text: str, query: str) -> str:
    """Подсвечивает слова из запроса в тексте с использованием морфологии."""
    clean_query = re.sub(r"(video_id|v):[^\s]+", "", query).strip()
    query_words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]+", clean_query.lower())
    if not query_words:
        return text

    lemmas = set()
    for w in query_words:
        if len(w) < 3:
            continue
        lemmas.add(_morph.parse(w)[0].normal_form)
        lemmas.add(w)

    parts = re.split(r"([а-яА-ЯёЁa-zA-Z0-9]+)", text)
    result = []
    for part in parts:
        if not part:
            continue
        if re.match(r"([а-яА-ЯёЁa-zA-Z0-9]+)", part):
            word_lower = part.lower()
            word_lemma = _morph.parse(word_lower)[0].normal_form
            if word_lower in lemmas or word_lemma in lemmas:
                result.append(f"<mark>{part}</mark>")
            else:
                result.append(part)
        else:
            result.append(part)
    return "".join(result)


def build_quote_regex(phrase: str) -> str:
    """Строит морфологическое регулярное выражение для точного совпадения фразы."""
    words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]+", phrase.lower())
    if not words:
        return ""
    regex_parts = []
    for w in words:
        if len(w) <= 3:
            root = w
            regex_parts.append(rf"\b{re.escape(root)}[ёе]*\b")
        elif len(w) <= 5:
            root = w[:-1]
            regex_parts.append(rf"\b{re.escape(root)}[а-яА-ЯёЁa-zA-Z0-9]*\b")
        else:
            drop_len = 2 if len(w) < 7 else 3
            root = w[:-drop_len]
            regex_parts.append(rf"\b{re.escape(root)}[а-яА-ЯёЁa-zA-Z0-9]*\b")

    separator = r"(?:[^а-яА-ЯёЁa-zA-Z0-9]+[а-яА-ЯёЁa-zA-Z0-9]+){0,10}?[^а-яА-ЯёЁa-zA-Z0-9]+"
    return separator.join(regex_parts)


def quote_highlight(text: str, exact_phrases: list[str]) -> str:
    """Подсвечивает фразы в режиме цитаты."""
    if not exact_phrases:
        return text
    patterns = []
    for phrase in exact_phrases:
        pattern = build_quote_regex(phrase)
        if pattern:
            patterns.append(f"(?i)({pattern})")
    if not patterns:
        return text
    combined_pattern = "|".join(patterns)
    try:
        return re.sub(
            combined_pattern,
            lambda m: f"<mark>{m.group(0)}</mark>",
            text,
            flags=re.UNICODE,
        )
    except Exception:
        return text
