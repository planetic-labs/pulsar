import logging
from typing import Any

logger = logging.getLogger(__name__)


def chunk_from_utterances(
    utterances: list[dict[str, Any]], max_chars: int = 500, overlap_chars: int = 50, single_chunk: bool = False
) -> list[dict[str, Any]]:
    """
    Groups small utterances into semantic chunks of roughly max_chars.
    If single_chunk is True, merges ALL utterances into one.
    """
    if not utterances:
        return []

    if single_chunk:
        chunk_text = " ".join(u.get("text", "") or u.get("transcript", "") for u in utterances)
        return [
            {
                "chunk_index": 0,
                "start_sec": float(utterances[0]["start"]),
                "end_sec": float(utterances[-1]["end"]),
                "text": chunk_text,
            }
        ]

    chunks = []
    current_batch = []
    current_length = 0
    chunk_index = 0

    for u in utterances:
        text = str(u.get("text", "") or u.get("transcript", ""))
        current_batch.append(u)
        current_length += len(text)

        if current_length >= max_chars:
            # Close current chunk
            chunk_text = " ".join(u.get("text", "") or u.get("transcript", "") for u in current_batch)

            chunks.append(
                {
                    "chunk_index": chunk_index,
                    "start_sec": float(current_batch[0]["start"]),
                    "end_sec": float(current_batch[-1]["end"]),
                    "text": chunk_text,
                }
            )

            chunk_index += 1
            # Simple non-overlapping for now (can be improved with windowing)
            current_batch = []
            current_length = 0

    if current_batch:
        chunk_text = " ".join(u.get("text", "") or u.get("transcript", "") for u in current_batch)
        chunks.append(
            {
                "chunk_index": chunk_index,
                "start_sec": float(current_batch[0]["start"]),
                "end_sec": float(current_batch[-1]["end"]),
                "text": chunk_text,
            }
        )

    return chunks


def batch_texts_for_embedding(texts: list[str]) -> list[list[str]]:
    """Simple chunking for API limits if needed."""
    # Filter empty
    valid_indices = [idx for idx, t in enumerate(texts) if t.strip()]
    if not valid_indices:
        return []

    valid_texts = [texts[idx] for idx in valid_indices]
    return [valid_texts]
