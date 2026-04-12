import re
import json
import time
from dataclasses import dataclass
import psycopg
from app.config import get_google_ai_settings
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from cachetools import LRUCache

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
    text: str
    combined_score: float
    match_type: str
    lexical_score: float = 0.0
    vector_score: float = 0.0
    engine: str = "unknown"
    alternative_texts: dict[str, str] = None

class GoogleEmbeddingClient:
    """Client for Google Generative AI (Gemini) Embeddings."""
    _cache = LRUCache(maxsize=1000)

    def __init__(self):
        self.settings = get_google_ai_settings()
        self.api_url = f"https://generativelanguage.googleapis.com/v1beta/{self.settings.embedding_model}:embedContent?key={self.settings.api_key}"

    def embed_text(self, text: str, is_query: bool = True) -> list[float]:
        # Cache key includes is_query since taskType differs
        cache_key = (text, is_query)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Google text-embedding-004 handles query/document distinction via taskType
        task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        
        payload = {
            "model": self.settings.embedding_model,
            "content": {
                "parts": [{"text": text}]
            },
            "taskType": task_type,
            "outputDimensionality": 768  # Match our Postgres vector size
        }
        
        req = Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        
        max_retries = 5
        base_delay = 1.0  # seconds
        
        for attempt in range(max_retries):
            try:
                with urlopen(req) as response:
                    result = json.loads(response.read().decode("utf-8"))
                    embedding = result["embedding"]["values"]
                    self._cache[cache_key] = embedding
                    return embedding
            except HTTPError as e:
                if e.code == 429 and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    time.sleep(delay)
                    continue
                raise e
            except Exception as e:
                # Re-raise to allow caller to handle retries/backoff
                raise e

def _simple_highlight(text: str, query: str) -> str:
    words = re.findall(r'\w+', query.lower())
    if not words: return text
    words.sort(key=len, reverse=True)
    highlighted = text
    for word in set(words):
        if len(word) < 3: continue
        pattern = re.compile(re.escape(word), re.IGNORECASE)
        highlighted = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", highlighted)
    return highlighted

def hybrid_search(
    connection: psycopg.Connection,
    query: str,
    *,
    limit: int = 20,
    mode: str = "default",
) -> list[SearchResult]:
    # 1. Get Query Embedding from Google
    client = GoogleEmbeddingClient()
    query_vector = client.embed_text(query, is_query=True)

    # 2. RRF (Reciprocal Rank Fusion) Query
    # Lexical Search CTE
    lexical_query = """
    lexical_search AS (
        SELECT c.id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(fts_tokens, websearch_to_tsquery('russian', %s)) DESC) as rank
        FROM chunks c
        JOIN transcripts t ON t.id = c.transcript_id
        WHERE t.is_primary = TRUE AND fts_tokens @@ websearch_to_tsquery('russian', %s)
        LIMIT 100
    )"""

    # Vector Search CTE
    vector_query = """
    vector_search AS (
        SELECT c.id, ROW_NUMBER() OVER (ORDER BY embedding <=> %s::vector) as rank
        FROM chunks c
        JOIN transcripts t ON t.id = c.transcript_id
        WHERE t.is_primary = TRUE AND embedding IS NOT NULL
        LIMIT 100
    )"""

    sql = f"""
    WITH {lexical_query}, {vector_query}
    SELECT 
        c.id as chunk_id,
        c.video_id,
        c.transcript_id,
        c.chunk_index,
        c.start_sec,
        c.end_sec,
        c.text,
        v.title,
        v.source_file_id,
        v.source_url,
        t.engine,
        COALESCE(1.0 / (l.rank + 60), 0.0) as l_score,
        COALESCE(1.0 / (vec.rank + 60), 0.0) as v_score,
        COALESCE(1.0 / (l.rank + 60), 0.0) + COALESCE(1.0 / (vec.rank + 60), 0.0) as rrf_score,
        CASE 
            WHEN l.id IS NOT NULL AND vec.id IS NOT NULL THEN 'hybrid'
            WHEN l.id IS NOT NULL THEN 'keyword'
            ELSE 'semantic'
        END as match_type
    FROM chunks c
    JOIN videos v ON v.id = c.video_id
    JOIN transcripts t ON t.id = c.transcript_id
    LEFT JOIN lexical_search l ON l.id = c.id
    LEFT JOIN vector_search vec ON vec.id = c.id
    WHERE l.id IS NOT NULL OR vec.id IS NOT NULL
    ORDER BY rrf_score DESC
    LIMIT %s
    """
    params = (query, query, query_vector, limit)
    
    rows = connection.execute(sql, params).fetchall()
    
    results = []
    for row in rows:
        # Всегда используем ПОЛНЫЙ текст блока и подсвечиваем слова в нем
        full_text = row["text"]
        highlighted_text = _simple_highlight(full_text, query)

        results.append(SearchResult(
            chunk_id=row["chunk_id"],
            video_id=row["video_id"],
            transcript_id=row["transcript_id"],
            title=row["title"],
            source_file_id=row["source_file_id"],
            source_url=row["source_url"],
            chunk_index=row["chunk_index"],
            start_sec=float(row["start_sec"]),
            end_sec=row["end_sec"],
            text=highlighted_text,
            combined_score=float(row["rrf_score"]),
            lexical_score=float(row["l_score"]),
            vector_score=float(row["v_score"]),
            match_type=row["match_type"],
            engine=str(row["engine"]),
            alternative_texts={}
        ))
    return results

def get_alternative_transcripts(
    connection: psycopg.Connection,
    video_id: int,
    target_sec: float,
    window_sec: float = 30.0
) -> dict[str, str]:
    """Fetches chunks from all transcripts of a video around a specific timestamp."""
    sql = """
        SELECT t.engine, c.text, c.start_sec, c.end_sec
        FROM chunks c
        JOIN transcripts t ON t.id = c.transcript_id
        WHERE c.video_id = %s 
          AND c.start_sec >= %s - %s
          AND c.start_sec <= %s + %s
        ORDER BY t.engine, c.start_sec ASC
    """
    rows = connection.execute(sql, (video_id, target_sec, window_sec, target_sec, window_sec)).fetchall()
    
    engine_texts = {}
    for row in rows:
        eng = row["engine"]
        if eng not in engine_texts:
            engine_texts[eng] = []
        engine_texts[eng].append(row["text"])
    
    # Join snippets for each engine
    return {eng: " ".join(texts) for eng, texts in engine_texts.items()}

def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
