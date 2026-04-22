import re
import json
import time
from dataclasses import dataclass, field
import sqlite3
from app.config import get_gemini_settings, get_qdrant_settings
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from cachetools import LRUCache
from qdrant_client import models
from app.qdrant import get_qdrant_client, get_sparse_embedding_model
from app.gemini import GeminiEmbeddingClient

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

import pymorphy3
_morph = pymorphy3.MorphAnalyzer()

def _simple_highlight(text: str, query: str) -> str:
    # Remove filters from query for highlighting
    clean_query = re.sub(r'(video_id|speaker|s|v):[^\s]+', '', query).strip()
    query_words = re.findall(r'[а-яА-ЯёЁa-zA-Z0-9]+', clean_query.lower())
    if not query_words: return text
    lemmas = set()
    for w in query_words:
        if len(w) < 3: continue
        lemmas.add(_morph.parse(w)[0].normal_form)
        lemmas.add(w)
    parts = re.split(r'([а-яА-ЯёЁa-zA-Z0-9]+)', text)
    result = []
    for part in parts:
        if not part: continue
        if re.match(r'[а-яА-ЯёЁa-zA-Z0-9]+', part):
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
    if hours: return f"{hours:02d}:{minutes:02d}:{secs:02d}"
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
    video_filter = None
    speaker_filter = None
    
    # Video ID Filter
    v_match = re.search(r'(?:video_id|v):(\d+)', query)
    if v_match:
        v_id = int(v_match.group(1))
        video_filter = models.FieldCondition(key="video_id", match=models.MatchValue(value=v_id))
    
    # Speaker Filter
    s_match = re.search(r'(?:speaker|s):([^\s]+)', query)
    if s_match:
        s_name = s_match.group(1).lower()
        # Find tags for this speaker name in DB
        rows = connection.execute(
            "SELECT video_id, speaker_tag FROM speakers WHERE LOWER(name) LIKE ?",
            (f"%{s_name}%",)
        ).fetchall()
        if rows:
            # Create a complex filter: (video_id=X AND speaker=Y) OR (video_id=Z AND speaker=W)
            conditions = []
            for r in rows:
                conditions.append(models.Filter(
                    must=[
                        models.FieldCondition(key="video_id", match=models.MatchValue(value=r["video_id"])),
                        models.FieldCondition(key="speaker", match=models.MatchText(text=r["speaker_tag"]))
                    ]
                ))
            speaker_filter = models.Filter(should=conditions)
        else:
            # If no speaker found by name, try to match by tag directly
            speaker_filter = models.FieldCondition(key="speaker", match=models.MatchText(text=s_name))

    # Clean query from filters for vector search
    clean_query = re.sub(r'(?:video_id|speaker|s|v):[^\s]+', '', query).strip()
    
    # Build Qdrant Filter
    must_filters = []
    if video_filter: must_filters.append(video_filter)
    if speaker_filter: must_filters.append(speaker_filter)
    
    q_filter = models.Filter(must=must_filters) if must_filters else None

    if q_filter and not clean_query:
        # Filter only search
        points = qdrant.scroll(
            collection_name=settings.collection_name,
            scroll_filter=q_filter,
            limit=limit,
            with_payload=True,
        )[0]
    else:
        # Hybrid Search with Filter
        dense_client = GeminiEmbeddingClient(get_gemini_settings())
        query_dense = dense_client.embed_text(clean_query or "video", task_type="RETRIEVAL_QUERY")
        sparse_model = get_sparse_embedding_model()
        query_sparse_gen = list(sparse_model.embed([clean_query or "video"]))[0]
        query_sparse = models.SparseVector(
            indices=query_sparse_gen.indices.tolist(),
            values=query_sparse_gen.values.tolist()
        )

        points = qdrant.query_points(
            collection_name=settings.collection_name,
            prefetch=[
                models.Prefetch(query=query_dense, using="default", limit=100, filter=q_filter),
                models.Prefetch(query=query_sparse, using="text-sparse", limit=100, filter=q_filter),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=True,
        ).points

    # 3. Get Speaker Mappings for results
    video_ids = list(set(p.payload.get("video_id") for p in points))
    speaker_map = {} # (video_id, tag) -> name
    if video_ids:
        placeholders = ",".join(["?"] * len(video_ids))
        rows = connection.execute(
            f"SELECT video_id, speaker_tag, name FROM speakers WHERE video_id IN ({placeholders})",
            video_ids
        ).fetchall()
        for r in rows:
            speaker_map[(r["video_id"], r["speaker_tag"])] = r["name"]

    results = []
    for point in points:
        payload = point.payload
        full_text = payload.get("text", "")
        highlighted_text = _simple_highlight(full_text, query)
        
        # Map speaker tags to names
        v_id = payload.get("video_id")
        raw_tags = payload.get("speaker") or ""
        mapped_names = []
        for tag in str(raw_tags).split(", "):
            if not tag: continue
            name = speaker_map.get((v_id, tag.strip()))
            mapped_names.append(name if name else f"Speaker {tag}")
        
        score = getattr(point, "score", 0.0) or 0.0
        
        results.append(SearchResult(
            chunk_id=payload.get("chunk_id"),
            video_id=v_id,
            transcript_id=payload.get("transcript_id"),
            title=payload.get("title"),
            source_file_id=payload.get("source_file_id"),
            source_url=payload.get("source_url"),
            chunk_index=int(payload.get("chunk_index", 0)),
            start_sec=float(payload.get("start_sec", 0.0)),
            end_sec=float(payload.get("end_sec", 0.0)),
            start_ts=format_timestamp(payload.get("start_sec", 0.0)),
            end_ts=format_timestamp(payload.get("end_sec", 0.0)),
            text=highlighted_text,
            combined_score=float(score),
            match_type="hybrid" if clean_query else "filter",
            raw_text=full_text,
            lexical_score=0.0,
            semantic_score=float(score) if clean_query else 0.0,
            vector_score=float(score) if clean_query else 0.0,
            speaker=", ".join(mapped_names) if mapped_names else None,
            alternative_texts={}
        ))

    # 4. Sort by time if it's a single video filter without query
    if video_filter and not clean_query:
        results.sort(key=lambda x: x.chunk_index)

    return results

def get_alternative_transcripts(
    connection: sqlite3.Connection,
    video_id: int,
    target_sec: float,
    window_sec: float = 30.0
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
