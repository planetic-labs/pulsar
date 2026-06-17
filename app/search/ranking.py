from __future__ import annotations

from typing import Any

from app.ports import ScoredPoint


def rrf_merge(
    dense: list[ScoredPoint],
    sparse: list[ScoredPoint],
    k: int = 60,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion — чистая функция для слияния результатов векторного

    и полнотекстового поиска.
    """
    combined_scores: dict[int, float] = {}
    points_map: dict[int, ScoredPoint] = {}
    id_to_semantic_score: dict[int, float] = {}
    id_to_lexical_score: dict[int, float] = {}

    for rank, p in enumerate(dense, start=1):
        combined_scores[p["id"]] = combined_scores.get(p["id"], 0.0) + (1.0 / (k + rank))
        points_map[p["id"]] = p
        id_to_semantic_score[p["id"]] = p["score"]

    for rank, p in enumerate(sparse, start=1):
        combined_scores[p["id"]] = combined_scores.get(p["id"], 0.0) + (1.0 / (k + rank))
        points_map[p["id"]] = p
        id_to_lexical_score[p["id"]] = p["score"]

    candidates: list[dict[str, Any]] = []
    for pid, score in combined_scores.items():
        if pid in points_map:
            # Определение типа совпадения
            m_type = "hybrid"
            if pid in id_to_semantic_score and pid not in id_to_lexical_score:
                m_type = "semantic"
            elif pid in id_to_lexical_score and pid not in id_to_semantic_score:
                m_type = "keyword"

            candidates.append(
                {
                    "point": points_map[pid],
                    "combined": score,
                    "semantic": id_to_semantic_score.get(pid, 0.0),
                    "lexical": id_to_lexical_score.get(pid, 0.0),
                    "match_type": m_type,
                }
            )

    candidates.sort(key=lambda x: x["combined"], reverse=True)
    return candidates
