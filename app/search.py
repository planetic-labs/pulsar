import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymorphy3
from qdrant_client import models

from app.config import get_app_settings, get_embedding_settings, get_qdrant_settings
from app.gemini import UnifiedEmbeddingClient
from app.qdrant import get_qdrant_client

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    chunk_id: int
    video_id: int
    transcript_id: int
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
    raw_text: str = ""
    version: int = 42
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    vector_score: float = 0.0
    speaker: str | None = None
    alternative_texts: dict[str, str] = field(default_factory=dict)


_morph = pymorphy3.MorphAnalyzer()


def _get_utterances_for_transcript(transcript_id: int) -> list[dict]:
    """Fetch normalized utterances from disk for a given transcript."""
    # Find normalized_json_path from DB
    try:
        from app.config import get_sqlite_settings
        from app.db import db_connection

        with db_connection(get_sqlite_settings()) as conn:
            row = conn.execute("SELECT normalized_json_path FROM transcripts WHERE id = ?", (transcript_id,)).fetchone()
            if not row:
                return []
            path = Path(row["normalized_json_path"])
            if not path.exists():
                return []
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("utterances", []) or data.get("chunks", [])
    except Exception as e:
        logger.error(f"Error loading utterances: {e}")
        return []


def _simple_highlight(text: str, query: str) -> str:
    clean_query = re.sub(r"(video_id|speaker|s|v):[^\s]+", "", query).strip()
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
    # Use [\s\S] to match across any character including newlines.
    # Limit gap to 60 characters - enough for a few small words/punctuation between anchors.
    return r"[\s\S]{0,60}?".join(regex_parts)


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


def _map_char_offset_to_time(offset: int, chunks_data: list[dict]) -> float:
    """Map character offset in combined text to video time using the best available utterance."""
    current_pos = 0
    for chunk in chunks_data:
        text = chunk.get("text", "")
        # Skip if chunk doesn't exist (LEFT JOIN NULL)
        if chunk.get("start_sec") is None:
            current_pos += len(text) + 1
            continue

        utterances = chunk.get("utterances", [])

        # If we have detailed utterances, search within them
        if utterances:
            # Combined text for this chunk in the SQL query was chunk.text + " "
            # So we check if the offset falls into this chunk
            if offset < current_pos + len(text) + 1:
                chunk_offset = offset - current_pos

                # Find the utterance that contains this offset
                u_pos = 0
                for u in utterances:
                    u_text = u.get("text", "")
                    if chunk_offset <= u_pos + len(u_text):
                        # Found it! Utterances have exact timings from Deepgram
                        return float(u.get("start", 0.0))
                    u_pos += len(u_text) + 1 # +1 for space

                # If not found in utterances but in chunk, return chunk start
                return float(chunk.get("start_sec") or 0.0)
        else:
            # Fallback to linear interpolation if no utterances available
            s = float(chunk.get("start_sec") or 0.0)
            e = float(chunk.get("end_sec") or 0.0)
            chunk_len = len(text)
            if offset <= current_pos + chunk_len:
                local_offset = max(0, offset - current_pos)
                if chunk_len > 0:
                    ratio = local_offset / chunk_len
                    return s + ratio * (e - s)
                return s

        current_pos += len(text) + 1

    return float(chunks_data[-1].get("end_sec") or 0.0) if chunks_data else 0.0


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
    qdrant = get_qdrant_client()
    settings = get_qdrant_settings()

    # 1. Parse Filters
    video_filter: models.Condition | None = None
    speaker_filter: models.Condition | None = None
    date_filter: models.Condition | None = None
    type_filter: models.Condition | None = None

    if video_type == "short":
        type_filter = models.FieldCondition(key="is_short", match=models.MatchValue(value=True))
    elif video_type == "long":
        type_filter = models.FieldCondition(key="is_short", match=models.MatchValue(value=False))

    if date_from or date_to:
        # User requirement: ignore date filters for short videos
        if video_type != "short":
            # DatetimeRange expects date/datetime objects or strings depending on version,
            # but ty expects date | None.
            from datetime import datetime

            # Qdrant supports ISO strings, but to satisfy the type checker we can use datetime objects
            gte_dt = datetime.fromisoformat(f"{date_from}T00:00:00") if date_from and len(date_from) > 1 else None
            lte_dt = datetime.fromisoformat(f"{date_to}T23:59:59") if date_to and len(date_to) > 1 else None

            if gte_dt or lte_dt:
                date_filter = models.FieldCondition(
                    key="recorded_date",
                    range=models.DatetimeRange(
                        gte=gte_dt,
                        lte=lte_dt,
                    ),
                )

    v_match = re.search(r"(?:video_id|v):(\d+)", query)
    v_id = None
    if v_match:
        v_id = int(v_match.group(1))
        video_filter = models.FieldCondition(key="video_id", match=models.MatchValue(value=v_id))

    s_match = re.search(r"(?:speaker|s):([^\s]+)", query)
    if s_match:
        s_name = s_match.group(1).lower()
        rows = connection.execute(
            "SELECT video_id, speaker_tag FROM speakers WHERE LOWER(name) LIKE ?", (f"%{s_name}%",)
        ).fetchall()
        if rows:
            conditions: list[models.Condition] = []
            for r in rows:
                conditions.append(
                    models.Filter(
                        must=[
                            models.FieldCondition(key="video_id", match=models.MatchValue(value=r["video_id"])),
                            models.FieldCondition(key="speaker", match=models.MatchText(text=r["speaker_tag"])),
                        ]
                    )
                )
            speaker_filter = models.Filter(should=conditions)
        else:
            speaker_filter = models.FieldCondition(key="speaker", match=models.MatchText(text=s_name))

    clean_query = re.sub(r"(?:video_id|speaker|s|v):[^\s]+", "", query).strip()

    must_filters: list[models.Condition] = []
    if video_filter:
        must_filters.append(video_filter)
    if speaker_filter:
        must_filters.append(speaker_filter)
    if date_filter:
        must_filters.append(date_filter)
    if type_filter:
        must_filters.append(type_filter)

    q_filter = models.Filter(must=must_filters) if must_filters else None

    scores_map: dict[Any, dict[str, Any]] = {}
    points: list[models.ScoredPoint] | list[models.Record] = []

    if search_mode == "quote" and clean_query:
        # --- FAST TEXTUAL QUOTE SEARCH (Sliding Window) ---
        pattern = _build_quote_regex(clean_query)
        if not pattern:
            return []

        # We search in a combined text of 3 chunks and return the combined text itself
        sql = """
            SELECT
                c1.id,
                c1.text as t1, IFNULL(c2.text, '') as t2, IFNULL(c3.text, '') as t3,
                c1.start_sec as s1, c1.end_sec as e1,
                c2.start_sec as s2, c2.end_sec as e2,
                c3.start_sec as s3, c3.end_sec as e3,
                (c1.text || ' ' || IFNULL(c2.text, '') || ' ' || IFNULL(c3.text, '')) as combined_text
            FROM chunks c1
            LEFT JOIN chunks c2 ON c1.video_id = c2.video_id AND c2.chunk_index = c1.chunk_index + 1
            LEFT JOIN chunks c3 ON c1.video_id = c3.video_id AND c3.chunk_index = c1.chunk_index + 2
            WHERE 1=1
        """
        params = []
        # Optimization: use LIKE as a pre-filter for the first few words to speed up REGEXP
        words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]{3,}", clean_query.lower())
        for w in words[:2]:  # Only first two words for pre-filter speed
            sql += " AND (c1.text || ' ' || IFNULL(c2.text, '') || ' ' || IFNULL(c3.text, '')) LIKE ?"
            params.append(f"%{w}%")

        sql += " AND (c1.text || ' ' || IFNULL(c2.text, '') || ' ' || IFNULL(c3.text, '')) REGEXP ?"
        params.append(pattern)

        if v_id:
            sql += " AND c1.video_id = ?"
            params.append(v_id)

        rows = connection.execute(sql, params).fetchall()

        # Map ID -> Full Quote Data for results
        id_to_quote_data = {
            r["id"]: {
                "combined_text": r["combined_text"],
                "chunks_data": [
                    {"text": r["t1"], "start_sec": r["s1"], "end_sec": r["e1"]},
                    {"text": r["t2"], "start_sec": r["s2"], "end_sec": r["e2"]},
                    {"text": r["t3"], "start_sec": r["s3"], "end_sec": r["e3"]},
                ],
            }
            for r in rows
        }
        candidate_ids = list(id_to_quote_data.keys())

        if candidate_ids:
            # Fetch payload for metadata (start_sec, video_id, etc.)
            exact_conditions: list[models.Condition] = [models.HasIdCondition(has_id=candidate_ids)]
            if must_filters:
                exact_conditions.extend(must_filters)

            res_scroll = qdrant.scroll(
                collection_name=settings.collection_name,
                scroll_filter=models.Filter(must=exact_conditions),
                limit=limit,
                with_payload=True,
            )
            points = res_scroll[0]

            # Use combined text from SQL and assign high scores
            scores_map = {}
            for p in points:
                q_data = id_to_quote_data.get(p.id)
                scores_map[p.id] = {
                    "combined": 100.0,
                    "match_type": "quote",
                    "quote_phrases": [clean_query],
                    "override_text": q_data["combined_text"] if q_data else None,
                    "quote_data": q_data,
                }
        else:
            points = []

    elif q_filter and not clean_query:
        res_scroll = qdrant.scroll(
            collection_name=settings.collection_name,
            scroll_filter=q_filter,
            limit=limit,
            with_payload=True,
        )
        points = res_scroll[0]
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
            dense_results = qdrant.query_points(
                settings.collection_name,
                query_dense,
                "default",
                None,
                q_filter,
                limit=prefetch_limit,
                with_payload=True,
            ).points

        if search_mode in ["lexical", "hybrid"] and query_sparse:
            sparse_results = qdrant.query_points(
                settings.collection_name,
                models.SparseVector(indices=query_sparse.indices, values=query_sparse.values),
                "text-sparse",
                None,
                q_filter,
                limit=prefetch_limit,
                with_payload=True,
            ).points

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

        # 3. Final sorting and diversification
        candidates_list = []
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

        # Diversification: Limit per video
        video_counts = {}
        final_list = []
        for item in candidates_list:
            vid = item["point"].payload.get("video_id")
            count = video_counts.get(vid, 0)
            if count < 3:
                final_list.append(item)
                video_counts[vid] = count + 1

        points = [x["point"] for x in final_list[:limit]]
        scores_map = {x["point"].id: x for x in final_list[:limit]}

    # 1. Collect all video IDs to fetch missing metadata
    video_ids = list({p.payload.get("video_id") for p in points if p.payload})
    speaker_map = {}
    video_metadata = {}

    if video_ids:
        placeholders = ",".join(["?"] * len(video_ids))

        # Fetch Speakers
        rows_s = connection.execute(
            f"SELECT video_id, speaker_tag, name FROM speakers WHERE video_id IN ({placeholders})", video_ids
        ).fetchall()
        for r in rows_s:
            speaker_map[(r["video_id"], r["speaker_tag"])] = r["name"]

        # Fetch Video Metadata (source_file_id, title)
        rows_v = connection.execute(
            f"SELECT id, source_file_id, title FROM videos WHERE id IN ({placeholders})", video_ids
        ).fetchall()
        for r in rows_v:
            video_metadata[r["id"]] = {"source_file_id": r["source_file_id"], "title": r["title"]}

        results = []

    # Cache for utterances (transcript_id -> utterances)
    utterance_cache = {}

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

        # Text extraction (with override and cropping for quote mode)
        full_text = point_data.get("override_text") or str(payload.get("text") or "")

        transcript_id = int(payload.get("transcript_id") or 0)

        if m_type == "quote":
            q_data = point_data.get("quote_data")
            if q_data and transcript_id:
                # Load utterances if not cached
                if transcript_id not in utterance_cache:
                    utterance_cache[transcript_id] = _get_utterances_for_transcript(transcript_id)

                uts = utterance_cache[transcript_id]
                if uts:
                    # Distribute utterances to chunks in q_data
                    # This is slightly complex because chunks are combined t1 + " " + t2 + " " + t3
                    # We need to know which utterances belong to which chunk index.

                    # Fetch chunk indexes for c1, c2, c3
                    c1_idx = int(payload.get("chunk_index") or 0)

                    # Group utterances by chunk index (this is simplified as we assume they were grouped by chunk_from_utterances)
                    # For Shorts, there's only 1 chunk with index 0.
                    # For others, we'll try to find utterances for c1_idx, c1_idx+1, c1_idx+2
                    for i, chunk in enumerate(q_data["chunks_data"]):
                        # Skip if chunk doesn't exist (e.g. LEFT JOIN returned NULL for s1/s2/s3)
                        if chunk["start_sec"] is None:
                            chunk["utterances"] = []
                            continue

                        # Heuristic: find utterances that fall within this chunk's time range
                        chunk["utterances"] = [
                            u for u in uts
                            if float(u.get("start", 0.0)) >= chunk["start_sec"] - 0.01
                            and float(u.get("end", 0.0)) <= chunk["end_sec"] + 0.01
                        ]
            # For quote mode, crop around the best match
            pattern = _build_quote_regex(clean_query)
            if pattern:
                query_words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]{3,}", clean_query.lower())
                q_word_count = len(query_words) if query_words else 1
                full_text = _crop_around_match(full_text, pattern, q_word_count)

        # Use standard individual-word highlighting for everyone, but quote-aware for quote mode
        if m_type == "quote":
            highlighted_text = _quote_highlight(full_text, [clean_query])
        else:
            highlighted_text = _simple_highlight(full_text, clean_query)

        v_id = payload.get("video_id")
        v_meta = video_metadata.get(v_id, {})

        # --- FIX: Ensure we have current title, source_file_id and source_url from DB ---
        title = v_meta.get("title") or str(payload.get("title") or "Unknown Video")
        source_file_id = v_meta.get("source_file_id") or payload.get("source_file_id")
        source_url = payload.get("source_url")
        if not source_url and source_file_id:
            source_url = f"https://drive.google.com/file/d/{source_file_id}/view"

        raw_tags = payload.get("speaker") or ""
        mapped_names = []
        for tag in str(raw_tags).split(", "):
            if not tag:
                continue
            name = speaker_map.get((v_id, tag.strip()))
            mapped_names.append(name if name else f"Speaker {tag}")

        def get_float(p_load, key, default=0.0):
            val = p_load.get(key)
            if val is None:
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        def get_int(p_load, key, default=0):
            val = p_load.get(key)
            if val is None:
                return default
            try:
                return int(val)
            except (ValueError, TypeError):
                return default

        chunk_id = get_int(payload, "chunk_id") or get_int(payload, "id")
        start_sec = get_float(payload, "start_sec")
        end_sec = get_float(payload, "end_sec")

        if m_type == "quote":
            q_data = point_data.get("quote_data")
            if q_data and clean_query:
                pattern = _build_quote_regex(clean_query)
                if pattern:
                    query_words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]{3,}", clean_query.lower())
                    q_word_count = len(query_words) if query_words else 1
                    # Precise timing: map character offset of the match to seconds
                    q_start, q_end, q_prec = _find_best_match(q_data["combined_text"], pattern, q_word_count)
                    if q_prec > 0:
                        start_sec = _map_char_offset_to_time(q_start, q_data["chunks_data"])
                        end_sec = _map_char_offset_to_time(q_end, q_data["chunks_data"])

        results.append(
            SearchResult(
                chunk_id=chunk_id,
                video_id=get_int(payload, "video_id"),
                transcript_id=get_int(payload, "transcript_id"),
                title=title,
                source_file_id=source_file_id,
                source_url=source_url,
                chunk_index=get_int(payload, "chunk_index"),
                start_sec=start_sec,
                end_sec=end_sec,
                start_ts=format_timestamp(start_sec),
                end_ts=format_timestamp(end_sec),
                text=highlighted_text,
                combined_score=combined_score,
                match_type=m_type,
                recorded_date=payload.get("recorded_date"),
                is_short=bool(payload.get("is_short", False)),
                raw_text=full_text,
                lexical_score=lexical_score,
                semantic_score=semantic_score,
                vector_score=combined_score,
                speaker=", ".join(mapped_names) if mapped_names else None,
                alternative_texts={},
            )
        )

    if video_filter and not clean_query:
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
