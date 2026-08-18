from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.manticore import int_to_date
from app.ports import EmbeddingPort, VectorStorePort
from app.repos.cache_repo import CacheRepository
from app.repos.video_repo import VideoRepository
from app.search.filters import build_manticore_phrase_query, build_where_clause
from app.search.highlighter import quote_highlight, simple_highlight
from app.search.ranking import rrf_merge
from app.settings import Settings

logger = logging.getLogger("app.services.search")


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
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    vector_score: float = 0.0
    speaker: str | None = None
    alternative_texts: dict[str, str] = field(default_factory=dict)
    is_flagged: bool = False


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _get_float(p_load: dict[str, Any], key: str, default: float = 0.0) -> float:
    val = p_load.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError, TypeError:
        return default


def _get_int(p_load: dict[str, Any], key: str, default: int = 0) -> int:
    val = p_load.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError, TypeError:
        return default


class SearchService:
    """Сервис для выполнения гибридного поиска по видеозаписям."""

    def __init__(
        self,
        embedder: EmbeddingPort,
        vector_store: VectorStorePort,
        video_repo: VideoRepository,
        cache_repo: CacheRepository,
        settings: Settings,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.video_repo = video_repo
        self.cache_repo = cache_repo
        self.settings = settings

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        search_mode: str = "hybrid",
        date_from: str | None = None,
        date_to: str | None = None,
        video_type: str = "all",
    ) -> list[SearchResult]:
        # 1. Построение WHERE-условий
        where_clause, v_id = build_where_clause(video_type, date_from, date_to, query)
        clean_query = re.sub(r"(?:video_id|v):[^\s]+", "", query).strip()

        scores_map: dict[int, dict[str, Any]] = {}
        points: list[Any] = []

        if search_mode == "quote" and clean_query:
            # Фразовый поиск
            phrase_query = build_manticore_phrase_query(clean_query, slop=10)
            if not phrase_query:
                return []

            raw_points = await self.vector_store.search_fulltext(
                table=self.settings.manticore_table,
                query=phrase_query,
                limit=limit * 3,
                where=where_clause,
            )
            # Приведение к формату Manticore Record
            points = [dict(p) for p in raw_points]
            scores_map = {
                int(p["id"]): {
                    "combined": 100.0,
                    "match_type": "quote",
                }
                for p in points
            }

        elif where_clause and not clean_query:
            # Только фильтрация (без текста)
            points = await self.vector_store.filter_only(self.settings.manticore_table, where_clause, limit)
            scores_map = {int(p["id"]): {"combined": 1.0, "match_type": "filter"} for p in points}

        else:
            # Гибридный/семантический/лексический поиск
            try:
                query_dense, _ = await self.embedder.embed_text(clean_query or "video")
            except Exception as e:
                logger.error(f"Search failed because embedding service is unavailable: {e}")
                return []

            prefetch_limit = 100
            dense_task: asyncio.Task[Any] = asyncio.create_task(asyncio.sleep(0, []))
            sparse_task: asyncio.Task[Any] = asyncio.create_task(asyncio.sleep(0, []))

            if search_mode in ["semantic", "hybrid"]:
                dense_task = asyncio.create_task(
                    self.vector_store.search_vectors(
                        self.settings.manticore_table,
                        query_dense,
                        limit=prefetch_limit,
                        where=where_clause,
                    )
                )

            if search_mode in ["lexical", "hybrid"] and clean_query:
                sparse_task = asyncio.create_task(
                    self.vector_store.search_fulltext(
                        self.settings.manticore_table,
                        clean_query,
                        limit=prefetch_limit,
                        where=where_clause,
                    )
                )

            dense_results, sparse_results = await asyncio.gather(dense_task, sparse_task)

            # Объединение RRF
            candidates = rrf_merge(dense_results, sparse_results)
            points = [{"id": c["point"]["id"], "payload": c["point"]["payload"]} for c in candidates]
            scores_map = {int(c["point"]["id"]): c for c in candidates}

        # Гидратация метаданных
        video_ids = list({p["payload"].get("video_id") for p in points if p.get("payload")})
        video_metadata = {}
        if video_ids:
            # Получаем метаданные видео через репозиторий
            valid_vids: set[int] = set()
            for vid in video_ids:
                if vid is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        valid_vids.add(int(str(vid)))
            video_metadata = await self.video_repo.get_metadata_batch(valid_vids)

        # Получаем список помеченных чанков
        flagged_chunk_ids = set()
        chunk_ids = []
        for p in points:
            payload = p.get("payload")
            if payload:
                chunk_ids.append(_get_int(payload, "chunk_id") or int(p["id"]))
        if chunk_ids:
            try:
                placeholders = ",".join("?" for _ in chunk_ids)
                sql = f"SELECT chunk_id FROM subtitle_flags WHERE chunk_id IN ({placeholders})"
                async with (
                    self.video_repo.db.transaction() as conn,
                    conn.execute(sql, tuple(chunk_ids)) as cursor,
                ):
                    rows = await cursor.fetchall()
                    flagged_chunk_ids = {int(row["chunk_id"]) for row in rows}
            except Exception as e:
                logger.error(f"Failed to query flagged chunks: {e}")

        results = []
        for point in points:
            payload = point.get("payload")
            if not payload:
                continue

            point_id = int(point["id"])
            point_data = scores_map.get(point_id) or {}
            combined_score = float(point_data.get("combined", 0.0))
            semantic_score = float(point_data.get("semantic", 0.0))
            lexical_score = float(point_data.get("lexical", 0.0))
            m_type = point_data.get("match_type", "hybrid" if clean_query else "filter")

            full_text = str(payload.get("text") or "")
            raw_highlighted = payload.get("highlighted_text")
            highlighted_text = str(raw_highlighted) if raw_highlighted else ""
            if not highlighted_text:
                if m_type == "quote":
                    highlighted_text = quote_highlight(full_text, [clean_query])
                else:
                    highlighted_text = simple_highlight(full_text, clean_query)

            point_v_id = payload.get("video_id")
            v_id_int = None
            if point_v_id is not None:
                with contextlib.suppress(ValueError, TypeError):
                    v_id_int = int(str(point_v_id))
            v_meta = video_metadata.get(v_id_int, {}) if v_id_int is not None else {}

            title = str(v_meta.get("title") or payload.get("title") or "Unknown Video")
            raw_source_file_id = v_meta.get("source_file_id") or payload.get("source_file_id")
            source_file_id = str(raw_source_file_id) if raw_source_file_id is not None else None
            raw_source_url = payload.get("source_url")
            source_url = str(raw_source_url) if raw_source_url else None
            if not source_url and source_file_id:
                source_url = f"https://drive.google.com/file/d/{source_file_id}/view"

            chunk_id = _get_int(payload, "chunk_id") or point_id
            start_sec = _get_float(payload, "start_sec")
            end_sec = _get_float(payload, "end_sec")

            rec_date_raw = payload.get("recorded_date")
            rec_date_int = None
            if rec_date_raw is not None:
                with contextlib.suppress(ValueError, TypeError):
                    rec_date_int = int(str(rec_date_raw))
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
                    is_flagged=(chunk_id in flagged_chunk_ids),
                )
            )

        # Постобработка: Дедупликация -> Диверсификация -> Ограничение
        # А. Дедупликация по времени
        seen_timing = set()
        unique_results = []
        for res in results:
            key = (res.video_id, round(res.start_sec * 2) / 2)
            if key not in seen_timing:
                seen_timing.add(key)
                unique_results.append(res)
        results = unique_results

        # Б. Диверсификация (макс 3 результата на видео)
        video_counts = {}
        diversified = []
        for res in results:
            count = video_counts.get(res.video_id, 0)
            if count < 3 or (v_id is not None):
                diversified.append(res)
                video_counts[res.video_id] = count + 1
        results = diversified

        # В. Финальный лимит
        results = results[:limit]

        if v_id and not clean_query:
            results.sort(key=lambda x: x.chunk_index)

        return results
