import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pymorphy3

from app.config import get_embedding_settings, get_manticore_settings
from app.embeddings import UnifiedEmbeddingClient
from app.manticore import date_to_int, get_manticore_client, int_to_date, models

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    chunk_id: int
    video_id: int
    title: str
    source_file_id: str | None
    source_url: str | None
    chunk_index: int
    start_sec: float
    end_sec: float
    start_ts: str
    end_ts: str
    text: str
    combined_score: float
    match_type: str
    recorded_date: str | None = None
    is_short: bool = False
    is_4k: bool = False
    raw_text: str = ""
    version: int = 42
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    vector_score: float = 0.0
    speaker: str | None = None
    alternative_texts: dict[str, str] = field(default_factory=dict)


_morph = pymorphy3.MorphAnalyzer()


def _get_float(p_load: dict[str, Any], key: str, default: float = 0.0) -> float:
    val = p_load.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _get_int(p_load: dict[str, Any], key: str, default: int = 0) -> int:
    val = p_load.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _simple_highlight(text: str, query: str) -> str:
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


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _build_quote_regex(phrase: str) -> str:
    """Build a morphological regex that allows gaps between words in sequence."""
    words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]+", phrase.lower())
    if not words:
        return ""
    regex_parts = []
    for w in words:
        if len(w) <= 3:
            # For short words, be very strict
            root = w
            regex_parts.append(rf"\b{re.escape(root)}[ёе]*\b")
        elif len(w) <= 5:
            # Drop 1 char for medium
            root = w[:-1]
            regex_parts.append(rf"\b{re.escape(root)}[а-яА-ЯёЁa-zA-Z0-9]*\b")
        else:
            # Drop 2-3 for long
            drop_len = 2 if len(w) < 7 else 3
            root = w[:-drop_len]
            regex_parts.append(rf"\b{re.escape(root)}[а-яА-ЯёЁa-zA-Z0-9]*\b")
    # Limit gap to exactly 10 words (any sequence of alphanumeric characters) between the anchors.
    # [^а-яА-ЯёЁa-zA-Z0-9]+ matches punctuation and whitespace.
    separator = r"(?:[^а-яА-ЯёЁa-zA-Z0-9]+[а-яА-ЯёЁa-zA-Z0-9]+){0,10}?[^а-яА-ЯёЁa-zA-Z0-9]+"
    return separator.join(regex_parts)


def _build_manticore_phrase_query(query: str, slop: int = 10) -> str | None:
    """Build a Manticore Search proximity query syntax like '"word1 word2"~10'."""
    words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]+", query.lower())
    if not words:
        return None
    phrase = " ".join(words)
    return f'"{phrase}"~{slop}'


def _quote_highlight(text: str, exact_phrases: list[str]) -> str:
    """Highlight matches in quote mode using the same regex logic."""
    if not exact_phrases:
        return text
    patterns = []
    for phrase in exact_phrases:
        pattern = _build_quote_regex(phrase)
        if pattern:
            # Add (?i) for case-insensitivity within sub.
            patterns.append(f"(?i)({pattern})")
    if not patterns:
        return text
    combined_pattern = "|".join(patterns)
    try:
        # Use flags for unicode support
        return re.sub(combined_pattern, lambda m: f"<mark>{m.group(0)}</mark>", text, flags=re.UNICODE)
    except Exception:
        return text


def _find_best_match(text: str, pattern: str, query_words_count: int) -> tuple[int, int, float]:
    """Find the best match and return its span and a high-precision score."""
    try:
        matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.UNICODE | re.DOTALL))
        if not matches:
            return 0, len(text), 0.0

        best_m = matches[0]
        min_len = 100000

        for m in matches:
            curr_len = m.end() - m.start()
            if curr_len < min_len:
                min_len = curr_len
                best_m = m

        # Precision score:
        # If words are tight (length ~ words * 10), score is near 1.0.
        # If words are spread out, score drops rapidly.
        # We also consider how many words from query we are actually looking for.
        ideal_len = query_words_count * 8
        precision = (ideal_len / max(ideal_len, min_len)) ** 2

        return best_m.start(), best_m.end(), precision
    except Exception:
        return 0, len(text), 0.0


def _crop_around_match(text: str, pattern: str, query_words_count: int, window: int = 250) -> str:
    """Crop text around the best regex match."""
    start, end, precision = _find_best_match(text, pattern, query_words_count)

    # If no match found by regex, just show start
    if precision == 0:
        return text[: window * 2] + "..."

    # Find a good start point (e.g. at a space)
    crop_start = max(0, start - window)
    if crop_start > 0:
        first_space = text.find(" ", crop_start, start)
        if first_space != -1:
            crop_start = first_space + 1

    # Find a good end point
    crop_end = min(len(text), end + window)
    if crop_end < len(text):
        last_space = text.rfind(" ", end, crop_end)
        if last_space != -1:
            crop_end = last_space

    snippet = text[crop_start:crop_end]
    if crop_start > 0:
        snippet = "..." + snippet
    if crop_end < len(text):
        snippet = snippet + "..."
    return snippet


async def hybrid_search(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    search_mode: str = "hybrid",
    date_from: str | None = None,
    date_to: str | None = None,
    video_type: str = "all",
) -> list[SearchResult]:
    manticore = get_manticore_client()
    settings = get_manticore_settings()

    # Force lexical search since vec column was removed from Manticore
    if search_mode in ("semantic", "hybrid"):
        search_mode = "lexical"

    # 1. Parse Filters
    where_clauses: list[str] = []

    if video_type == "short":
        where_clauses.append("is_short = 1")
    elif video_type == "long":
        where_clauses.append("is_short = 0")
    elif video_type == "4k":
        where_clauses.append("is_4k = 1")

    if date_from or date_to:
        # User requirement: ignore date filters for short videos
        if video_type != "short":
            if date_from:
                where_clauses.append(f"recorded_date >= {date_to_int(date_from)}")
            if date_to:
                where_clauses.append(f"recorded_date <= {date_to_int(date_to)}")

    v_match = re.search(r"(?:video_id|v):(\d+)", query)
    v_id = None
    if v_match:
        v_id = int(v_match.group(1))
        where_clauses.append(f"video_id = {v_id}")

    clean_query = re.sub(r"(?:video_id|v):[^\s]+", "", query).strip()

    where_clause = " AND ".join(where_clauses) if where_clauses else None

    scores_map: dict[Any, dict[str, Any]] = {}
    points: list[models.ScoredPoint] | list[models.Record] = []

    if search_mode == "quote" and clean_query:
        # --- FAST INDEXED PHRASE SEARCH (Manticore) ---
        phrase_query = _build_manticore_phrase_query(clean_query, slop=10)
        if not phrase_query:
            return []

        # 1. Search in Manticore using Proximity Search
        points = manticore.query_points(
            collection_name=settings.table_name,
            query=phrase_query,
            using="text",
            where_clause=where_clause,
            limit=limit * 3,
        )

        scores_map = {
            p.id: {
                "combined": 100.0,
                "match_type": "quote",
            }
            for p in points
        }

    elif where_clause and not clean_query:
        res_scroll = manticore.scroll(
            collection_name=settings.table_name,
            where_clause=where_clause,
            limit=limit,
        )
        points = res_scroll[0]
        scores_map = {p.id: {"combined": 1.0, "match_type": "filter"} for p in points}
    else:
        # --- VECTOR SEARCH (SEMANTIC, LEXICAL, HYBRID) ---
        client = UnifiedEmbeddingClient(get_embedding_settings())
        try:
            query_dense, query_sparse = await client.embed_text_async(
                clean_query or "video", task_type="RETRIEVAL_QUERY"
            )
        except Exception as e:
            import logging

            logging.error(f"Search failed because embedding service is unavailable: {e}")
            return []

        prefetch_limit = 100
        dense_results: list[models.ScoredPoint] = []
        sparse_results: list[models.ScoredPoint] = []

        # 1. Fetch results based on mode
        if search_mode in ["semantic", "hybrid"]:
            dense_results = manticore.query_points(
                settings.table_name,
                query_dense,
                using="default",
                where_clause=where_clause,
                limit=prefetch_limit,
            )

        if search_mode in ["lexical", "hybrid"] and clean_query:
            sparse_results = manticore.query_points(
                settings.table_name,
                clean_query,
                using="text",
                where_clause=where_clause,
                limit=prefetch_limit,
            )

        # 2. Merge results using RRF
        k = 60
        combined_scores: dict[Any, float] = {}
        points_map = {}
        id_to_semantic_score = {}
        id_to_lexical_score = {}

        for rank, p in enumerate(dense_results, start=1):
            combined_scores[p.id] = combined_scores.get(p.id, 0.0) + (1.0 / (k + rank))
            points_map[p.id] = p
            id_to_semantic_score[p.id] = p.score

        for rank, p in enumerate(sparse_results, start=1):
            combined_scores[p.id] = combined_scores.get(p.id, 0.0) + (1.0 / (k + rank))
            points_map[p.id] = p
            id_to_lexical_score[p.id] = p.score

        # 3. Final sorting
        candidates_list: list[dict[str, Any]] = []
        for pid, score in combined_scores.items():
            if pid in points_map:
                # Determine match type based on which results contained the point
                m_type = "hybrid"
                if search_mode == "semantic" or (pid in id_to_semantic_score and pid not in id_to_lexical_score):
                    m_type = "semantic"
                elif search_mode == "lexical" or (pid in id_to_lexical_score and pid not in id_to_semantic_score):
                    m_type = "keyword"

                candidates_list.append(
                    {
                        "point": points_map[pid],
                        "combined": score,
                        "semantic": id_to_semantic_score.get(pid, 0.0),
                        "lexical": id_to_lexical_score.get(pid, 0.0),
                        "match_type": m_type,
                    }
                )

        candidates_list.sort(key=lambda x: x["combined"], reverse=True)

        points = [x["point"] for x in candidates_list]
        scores_map = {x["point"].id: x for x in candidates_list}

    # 1. Collect all video IDs to fetch missing metadata
    video_ids = list({p.payload.get("video_id") for p in points if p.payload})
    video_metadata = {}

    if video_ids:
        placeholders = ",".join(["?"] * len(video_ids))

        # Fetch Video Metadata (source_file_id, title)
        rows_v = connection.execute(
            f"SELECT id, source_file_id, title FROM videos WHERE id IN ({placeholders})", video_ids
        ).fetchall()
        for r in rows_v:
            video_metadata[r["id"]] = {"source_file_id": r["source_file_id"], "title": r["title"]}

    results = []

    for point in points:
        payload = point.payload
        if not payload:
            continue

        # Get scores and match metadata from our map
        point_data = scores_map.get(point.id) or {}
        combined_score = float(point_data.get("combined", 0.0))
        semantic_score = float(point_data.get("semantic", 0.0))
        lexical_score = float(point_data.get("lexical", 0.0))
        m_type = point_data.get("match_type", "hybrid" if clean_query else "filter")

        full_text = str(payload.get("text") or "")
        highlighted_text = payload.get("highlighted_text")
        if not highlighted_text:
            # Fallback to local python regex highlighter if Manticore highlight is not available
            if m_type == "quote":
                highlighted_text = _quote_highlight(full_text, [clean_query])
            else:
                highlighted_text = _simple_highlight(full_text, clean_query)

        v_id = payload.get("video_id")
        v_meta = video_metadata.get(v_id, {})

        title = v_meta.get("title") or str(payload.get("title") or "Unknown Video")
        source_file_id = v_meta.get("source_file_id") or payload.get("source_file_id")
        source_url = payload.get("source_url")
        if not source_url and source_file_id:
            source_url = f"https://drive.google.com/file/d/{source_file_id}/view"

        chunk_id = _get_int(payload, "chunk_id") or _get_int(payload, "id")
        start_sec = _get_float(payload, "start_sec")
        end_sec = _get_float(payload, "end_sec")

        rec_date_raw = payload.get("recorded_date")
        rec_date_int = None
        if rec_date_raw is not None:
            try:
                rec_date_int = int(rec_date_raw)
            except (ValueError, TypeError):
                pass
        recorded_date_str = int_to_date(rec_date_int)

        results.append(
            SearchResult(
                chunk_id=chunk_id,
                video_id=_get_int(payload, "video_id"),
                title=title,
                source_file_id=source_file_id,
                source_url=source_url,
                chunk_index=_get_int(payload, "chunk_index"),
                start_sec=start_sec,
                end_sec=end_sec,
                start_ts=format_timestamp(start_sec),
                end_ts=format_timestamp(end_sec),
                text=highlighted_text,
                combined_score=combined_score,
                match_type=m_type,
                recorded_date=recorded_date_str,
                is_short=bool(payload.get("is_short", False)),
                is_4k=bool(payload.get("is_4k", False)),
                raw_text=full_text,
                lexical_score=lexical_score,
                semantic_score=semantic_score,
                vector_score=combined_score,
                speaker=None,
                alternative_texts={},
            )
        )

    # 2. Final pipeline: Deduplication -> Diversification -> Limit
    # A. Deduplication by timing (critical for Quote Search sliding window)
    seen_timing = set()
    unique_results = []
    for res in results:
        # Use 0.5s window to group very close matches (same phrase in different chunks)
        key = (res.video_id, round(res.start_sec * 2) / 2)
        if key not in seen_timing:
            seen_timing.add(key)
            unique_results.append(res)
    results = unique_results

    # B. Diversification (Limit results per video)
    # Default: max 3 results per video to keep variety
    video_counts = {}
    diversified = []
    for res in results:
        count = video_counts.get(res.video_id, 0)
        if count < 3 or (v_id is not None):  # Don't limit if searching within specific video
            diversified.append(res)
            video_counts[res.video_id] = count + 1
    results = diversified

    # C. Apply final limit
    results = results[:limit]

    if v_id and not clean_query:
        results.sort(key=lambda x: x.chunk_index)

    return results


def get_alternative_transcripts(
    connection: sqlite3.Connection, video_id: int, target_sec: float, window_sec: float = 30.0
) -> dict[str, str]:
    sql = """
        SELECT c.text, c.start_sec, c.end_sec
        FROM chunks c
        WHERE c.video_id = ?
          AND c.start_sec >= ? - ?
          AND c.start_sec <= ? + ?
        ORDER BY c.start_sec ASC
    """
    rows = connection.execute(sql, (video_id, target_sec, window_sec, target_sec, window_sec)).fetchall()
    return {"Deepgram": " ".join(r["text"] for r in rows)}
