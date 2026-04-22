from __future__ import annotations
from typing import Any
import numpy as np
from fastembed import TextEmbedding

# Initialize a small fast model for chunking decisions (local, no API needed)
_chunking_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5") # Good for cross-lingual too

def _cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

def chunk_from_utterances(
    utterances: list[dict[str, Any]], 
    min_chunk_words: int = 50,
    max_chunk_words: int = 150,
    similarity_threshold: float = 0.5
) -> list[dict[str, Any]]:
    """
    Groups utterances into chunks based on semantic similarity.
    Breaks when a new utterance is significantly different from current chunk context
    OR when max size is reached.
    """
    if not utterances:
        return []

    chunks = []
    current_batch = []
    current_word_count = 0
    chunk_index = 0
    
    # Pre-embed all utterances for faster comparison (small model is very fast)
    texts = [u.get("text", "") or u.get("transcript", "") for u in utterances]
    # Filter empty
    valid_indices = [idx for idx, t in enumerate(texts) if t.strip()]
    if not valid_indices: return []
    
    valid_texts = [texts[idx] for idx in valid_indices]
    embeddings = list(_chunking_model.embed(valid_texts))

    for i, idx in enumerate(valid_indices):
        utt = utterances[idx]
        text = valid_texts[i]
        emb = embeddings[i]
        
        words = text.split()
        num_words = len(words)
        
        should_split = False
        
        if current_batch:
            # 1. Check similarity with the last added item
            sim = _cosine_similarity(emb, embeddings[i-1])
            
            # 2. Logic: split if similarity is low AND we have enough words
            if sim < similarity_threshold and current_word_count >= min_chunk_words:
                should_split = True
            
            # 3. Logic: hard limit on size
            if current_word_count + num_words > max_chunk_words:
                should_split = True

        if should_split and current_batch:
            # Close current chunk
            chunk_text = " ".join(u.get("text", "") or u.get("transcript", "") for u in current_batch)
            speakers = set(str(u["speaker"]) for u in current_batch if u.get("speaker") is not None)
            
            chunks.append({
                "chunk_index": chunk_index,
                "start_sec": float(current_batch[0]["start"]),
                "end_sec": float(current_batch[-1]["end"]),
                "text": chunk_text.strip(),
                "speaker": ", ".join(sorted(speakers)) if speakers else None
            })
            chunk_index += 1
            current_batch = []
            current_word_count = 0

        current_batch.append(utt)
        current_word_count += num_words

    # Final chunk
    if current_batch:
        chunk_text = " ".join(u.get("text", "") or u.get("transcript", "") for u in current_batch)
        speakers = set(str(u["speaker"]) for u in current_batch if u.get("speaker") is not None)
        chunks.append({
            "chunk_index": chunk_index,
            "start_sec": float(current_batch[0]["start"]),
            "end_sec": float(current_batch[-1]["end"]),
            "text": chunk_text.strip(),
            "speaker": ", ".join(sorted(speakers)) if speakers else None
        })

    return chunks
