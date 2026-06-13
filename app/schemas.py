from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SearchResultItem(BaseModel):
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
    lexical_score: float
    semantic_score: float
    combined_score: float
    match_type: str
    raw_text: str = ""
    engine: str = "unknown"
    speaker: str | None = None
    alternative_texts: dict[str, str] | None = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResultItem]


class VideoStatusItem(BaseModel):
    id: int
    title: str
    source_file_id: str | None
    processing_status: str
    transcript_count: int
    chunk_count: int
    updated_at: datetime
    created_at: datetime | None = None
    primary_engine: str | None = None
