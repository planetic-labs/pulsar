from __future__ import annotations

import re

from app.manticore import date_to_int


def build_manticore_phrase_query(query: str, slop: int = 10) -> str | None:
    """Строит синтаксис фразового запроса для Manticore Search (например, '"слово1 слово2"~10')."""
    words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]+", query.lower())
    if not words:
        return None
    phrase = " ".join(words)
    return f'"{phrase}"~{slop}'


def build_where_clause(
    video_type: str,
    date_from: str | None,
    date_to: str | None,
    query: str,
) -> tuple[str | None, int | None]:
    """Строит WHERE-условие для Manticore Search на основе фильтров."""
    where_clauses: list[str] = []

    if video_type == "short":
        where_clauses.append("is_short = 1")
    elif video_type == "long":
        where_clauses.append("is_short = 0")
    elif video_type == "4k":
        where_clauses.append("is_4k = 1")

    # Игнорировать фильтры дат для коротких видео (требование ТЗ)
    if (date_from or date_to) and video_type != "short":
        if date_from:
            where_clauses.append(f"recorded_date >= {date_to_int(date_from)}")
        if date_to:
            where_clauses.append(f"recorded_date <= {date_to_int(date_to)}")

    # Фильтр по видео ID, если он передан в query (например, v:123)
    v_match = re.search(r"(?:video_id|v):(\d+)", query)
    v_id = None
    if v_match:
        v_id = int(v_match.group(1))
        where_clauses.append(f"video_id = {v_id}")

    where_clause = " AND ".join(where_clauses) if where_clauses else None
    return where_clause, v_id
