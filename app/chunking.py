import logging
from typing import Any

logger = logging.getLogger(__name__)


def chunk_from_utterances(
    utterances: list[dict[str, Any]], max_chars: int = 500, overlap_chars: int = 50
) -> list[dict[str, Any]]:
    """
    Groups small utterances into semantic chunks of roughly max_chars.
    """
    if not utterances:
        return []

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
            speakers = {str(u["speaker"]) for u in current_batch if u.get("speaker") is not None}

            chunks.append(
                {
                    "chunk_index": chunk_index,
                    "start_sec": float(current_batch[0]["start"]),
                    "end_sec": float(current_batch[-1]["end"]),
                    "text": chunk_text,
                    "speaker": ", ".join(sorted(speakers)) if speakers else None,
                }
            )

            chunk_index += 1
            # Simple non-overlapping for now (can be improved with windowing)
            current_batch = []
            current_length = 0

    if current_batch:
        chunk_text = " ".join(u.get("text", "") or u.get("transcript", "") for u in current_batch)
        speakers = {str(u["speaker"]) for u in current_batch if u.get("speaker") is not None}
        chunks.append(
            {
                "chunk_index": chunk_index,
                "start_sec": float(current_batch[0]["start"]),
                "end_sec": float(current_batch[-1]["end"]),
                "text": chunk_text,
                "speaker": ", ".join(sorted(speakers)) if speakers else None,
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
