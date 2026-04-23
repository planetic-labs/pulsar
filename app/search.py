import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pymorphy3
from qdrant_client import models

from app.config import get_embedding_settings, get_qdrant_settings
from app.gemini import UnifiedEmbeddingClient
from app.qdrant import get_qdrant_client


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
    raw_text: str = ""
    version: int = 42
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    vector_score: float = 0.0
    speaker: str | None = None
    alternative_texts: dict[str, str] = field(default_factory=dict)


_morph = pymorphy3.MorphAnalyzer()


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


def hybrid_search(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
) -> list[SearchResult]:
    qdrant = get_qdrant_client()
    settings = get_qdrant_settings()

    # 1. Parse Filters
    video_filter: models.Condition | None = None
    speaker_filter: models.Condition | None = None

    v_match = re.search(r"(?:video_id|v):(\d+)", query)
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

    q_filter = models.Filter(must=must_filters) if must_filters else None

    scores_map: dict[Any, dict[str, Any]] = {}
    points: list[models.ScoredPoint] | list[models.Record] = []

    if q_filter and not clean_query:
        res_scroll = qdrant.scroll(
            collection_name=settings.collection_name,
            scroll_filter=q_filter,
            limit=limit,
            with_payload=True,
        )
        points = res_scroll[0]
    else:
        # Hybrid Search via Unified Client
        client = UnifiedEmbeddingClient(get_embedding_settings())
        query_dense, query_sparse = client.embed_text(clean_query or "video", task_type="RETRIEVAL_QUERY")

        # Fetch candidates for merging
        prefetch_limit = 100

        # 1. Get Dense results
        dense_results = qdrant.query_points(
            settings.collection_name,
            query_dense,
            "default",
            None,
            q_filter,
            limit=prefetch_limit,
            with_payload=True,
        ).points

        # 2. Get Sparse results
        sparse_results: list[models.ScoredPoint] = []
        if query_sparse:
            sparse_results = qdrant.query_points(
                settings.collection_name,
                models.SparseVector(indices=query_sparse.indices, values=query_sparse.values),
                "text-sparse",
                None,
                q_filter,
                limit=prefetch_limit,
                with_payload=True,
            ).points

        # 3. Linear Fusion (Manual)
        # Weights: 0.7 Semantic (Dense), 0.3 Lexical (Sparse)
        w_dense = 0.7
        w_sparse = 0.3

        combined_points: dict[Any, dict[str, Any]] = {}

        for p in dense_results:
            combined_points[p.id] = {"point": p, "semantic": float(p.score), "lexical": 0.0}

        for p in sparse_results:
            if p.id in combined_points:
                combined_points[p.id]["lexical"] = float(p.score)
            else:
                combined_points[p.id] = {"point": p, "semantic": 0.0, "lexical": float(p.score)}

        # Calculate combined score and sort
        final_list = []
        for _pid, data in combined_points.items():
            # Linear combination. Semantic is Cosine (0-1), Sparse is Dot Product (can be > 1)
            # We cap Sparse to 1.0 for better fusion balance
            data["combined"] = (data["semantic"] * w_dense) + (min(float(data["lexical"]), 1.0) * w_sparse)
            final_list.append(data)

        final_list.sort(key=lambda x: float(x["combined"]), reverse=True)
        points = [x["point"] for x in final_list[:limit]]

        # Store scores for SearchResult mapping
        scores_map = {x["point"].id: x for x in final_list[:limit]}

    video_ids = list({p.payload.get("video_id") for p in points if p.payload})
    speaker_map = {}
    if video_ids:
        placeholders = ",".join(["?"] * len(video_ids))
        rows = connection.execute(
            f"SELECT video_id, speaker_tag, name FROM speakers WHERE video_id IN ({placeholders})", video_ids
        ).fetchall()
        for r in rows:
            speaker_map[(r["video_id"], r["speaker_tag"])] = r["name"]

    results = []
    for point in points:
        payload = point.payload
        if not payload:
            continue

        full_text = str(payload.get("text", ""))
        highlighted_text = _simple_highlight(full_text, query)

        v_id = payload.get("video_id")
        raw_tags = payload.get("speaker") or ""
        mapped_names = []
        for tag in str(raw_tags).split(", "):
            if not tag:
                continue
            name = speaker_map.get((v_id, tag.strip()))
            mapped_names.append(name if name else f"Speaker {tag}")

        # Get scores from our map if hybrid, else use point.score
        point_data = scores_map.get(point.id)

        results.append(
            SearchResult(
                chunk_id=int(payload.get("chunk_id", 0)),
                video_id=int(v_id or 0),
                transcript_id=int(payload.get("transcript_id", 0)),
                title=str(payload.get("title", "")),
                source_file_id=payload.get("source_file_id"),
                source_url=payload.get("source_url"),
                chunk_index=int(payload.get("chunk_index", 0)),
                start_sec=float(payload.get("start_sec", 0.0)),
                end_sec=float(payload.get("end_sec", 0.0)),
                start_ts=format_timestamp(float(payload.get("start_sec", 0.0))),
                end_ts=format_timestamp(float(payload.get("end_sec", 0.0))),
                text=highlighted_text,
                combined_score=float(point_data["combined"]) if point_data else float(getattr(point, "score", 0.0)),
                match_type="hybrid" if clean_query else "filter",
                raw_text=full_text,
                lexical_score=float(point_data["lexical"]) if point_data else 0.0,
                semantic_score=float(point_data["semantic"]) if point_data else float(getattr(point, "score", 0.0)),
                vector_score=float(point_data["combined"]) if point_data else float(getattr(point, "score", 0.0)),
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
