from __future__ import annotations
from typing import Any

def chunk_from_utterances(
    utterances: list[dict[str, Any]], 
    max_words_per_chunk: int = 80, 
    overlap_sentences: int = 1
) -> list[dict[str, Any]]:
    """
    Groups utterances into chunks by respecting sentence boundaries.
    Does NOT split sentences in the middle.
    """
    if not utterances:
        return []

    chunks = []
    current_chunk_utterances = []
    current_word_count = 0
    chunk_index = 0

    for i, utt in enumerate(utterances):
        text = utt.get("text", "") or utt.get("transcript", "")
        if not text.strip():
            continue
            
        words = text.split()
        num_words = len(words)
        
        current_chunk_utterances.append(utt)
        current_word_count += num_words

        # If we reached the limit or it's the last utterance
        if current_word_count >= max_words_per_chunk or i == len(utterances) - 1:
            chunk_text = " ".join(u.get("text", "") or u.get("transcript", "") for u in current_chunk_utterances)
            
            chunks.append({
                "chunk_index": chunk_index,
                "start_sec": float(current_chunk_utterances[0]["start"]),
                "end_sec": float(current_chunk_utterances[-1]["end"]),
                "text": chunk_text.strip()
            })
            
            chunk_index += 1
            
            # Start next chunk. If we want overlap, we keep some utterances.
            # For simplicity and clarity, we'll just keep the last sentence if overlap is requested.
            if overlap_sentences > 0 and i < len(utterances) - 1:
                # Keep only the last N utterances for context
                current_chunk_utterances = current_chunk_utterances[-overlap_sentences:]
                current_word_count = sum(len((u.get("text") or u.get("transcript", "")).split()) for u in current_chunk_utterances)
            else:
                current_chunk_utterances = []
                current_word_count = 0

    return chunks
