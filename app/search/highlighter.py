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


def _build_quote_word_patterns(phrase: str) -> list[str]:
    words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]+", phrase.lower())
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
    return regex_parts


def build_quote_regex(phrase: str) -> str:
    """Строит морфологическое регулярное выражение для точного совпадения фразы."""
    regex_parts = _build_quote_word_patterns(phrase)
    if not regex_parts:
        return ""

    separator = r"(?:[^а-яА-ЯёЁa-zA-Z0-9]+[а-яА-ЯёЁa-zA-Z0-9]+){0,10}?[^а-яА-ЯёЁa-zA-Z0-9]+"
    return separator.join(regex_parts)


def quote_highlight(text: str, exact_phrases: list[str]) -> str:
    """Подсвечивает только совпавшие слова цитаты, не затрагивая слова между ними."""
    if not exact_phrases:
        return text

    spans: list[tuple[int, int]] = []
    for phrase in exact_phrases:
        word_patterns = _build_quote_word_patterns(phrase)
        if not word_patterns:
            continue
        quote_pattern = build_quote_regex(phrase)
        try:
            quote_matches = re.finditer(quote_pattern, text, flags=re.IGNORECASE | re.UNICODE)
            for quote_match in quote_matches:
                cursor = quote_match.start()
                for word_pattern in word_patterns:
                    word_match = re.search(
                        word_pattern,
                        text[cursor : quote_match.end()],
                        flags=re.IGNORECASE | re.UNICODE,
                    )
                    if word_match is None:
                        break
                    start = cursor + word_match.start()
                    end = cursor + word_match.end()
                    spans.append((start, end))
                    cursor = end
        except re.error:
            continue

    if not spans:
        return text

    # Render against the original text so overlapping phrases cannot create nested <mark> tags.
    merged_spans: list[tuple[int, int]] = []
    for start, end in sorted(set(spans)):
        if merged_spans and start <= merged_spans[-1][1]:
            previous_start, previous_end = merged_spans[-1]
            merged_spans[-1] = (previous_start, max(previous_end, end))
        else:
            merged_spans.append((start, end))

    result = []
    cursor = 0
    for start, end in merged_spans:
        result.extend((text[cursor:start], "<mark>", text[start:end], "</mark>"))
        cursor = end
    result.append(text[cursor:])
    return "".join(result)
