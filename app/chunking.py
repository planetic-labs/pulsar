import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# Дефолтные настройки для экспериментов из переменных окружения
DEFAULT_PAUSE_THRESHOLD = float(os.getenv("CHUNKING_PAUSE_THRESHOLD", "3.0"))
DEFAULT_MIN_CHARS = int(os.getenv("CHUNKING_MIN_CHARS", "400"))
DEFAULT_MAX_CHARS = int(os.getenv("CHUNKING_MAX_CHARS", "2000"))
DEFAULT_ABSOLUTE_MAX_CHARS = int(os.getenv("CHUNKING_ABSOLUTE_MAX_CHARS", "3000"))
DEFAULT_OVERLAP_SENTENCES = int(os.getenv("CHUNKING_OVERLAP_SENTENCES", "1"))


def chunk_from_utterances(
    utterances: list[dict[str, Any]],
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
    absolute_max_chars: int = DEFAULT_ABSOLUTE_MAX_CHARS,
    pause_threshold: float = DEFAULT_PAUSE_THRESHOLD,
    overlap_sentences: int = DEFAULT_OVERLAP_SENTENCES,
    single_chunk: bool = False,
) -> list[dict[str, Any]]:
    """
    Группирует мелкие реплики (utterances) в семантические чанки на основе:
    1. Накопленного размера текста (не менее min_chars).
    2. Пауз между репликами (пауза >= pause_threshold секунд) — приоритетный разделитель.
    3. Границ предложений (знаки препинания ., ?, !), если накоплено не менее max_chars.
    4. Абсолютного лимита размера (absolute_max_chars).

    Если single_chunk=True, объединяет все реплики в один единственный чанк.
    """
    if not utterances:
        return []

    if single_chunk:
        chunk_text = " ".join(str(u.get("text", "") or u.get("transcript", "")).strip() for u in utterances)
        return [
            {
                "chunk_index": 0,
                "start_sec": float(utterances[0]["start"]),
                "end_sec": float(utterances[-1]["end"]),
                "text": chunk_text,
            }
        ]

    chunks_meta = []
    current_batch = []
    current_length = 0
    chunk_index = 0

    num_utterances = len(utterances)

    for i, u in enumerate(utterances):
        text = str(u.get("text", "") or u.get("transcript", "")).strip()
        if not text:
            continue

        current_batch.append(u)
        current_length += len(text)

        should_close = False
        closed_by_pause = False

        if current_length >= min_chars and i < num_utterances - 1:
            # Проверяем, заканчивается ли текущая реплика на знак препинания
            ends_sentence = text.endswith((".", "?", "!"))

            # Проверяем паузу перед следующей репликой
            next_u = utterances[i + 1]
            try:
                pause = float(next_u["start"]) - float(u["end"])
            except ValueError, TypeError, KeyError:
                pause = 0.0

            is_long_pause = pause >= pause_threshold

            # Закрываем, если встретили длинную паузу
            if is_long_pause:
                should_close = True
                closed_by_pause = True
            # Или если накопили max_chars и закончили предложение
            elif current_length >= max_chars and ends_sentence:
                should_close = True
            # Принудительное закрытие при достижении абсолютного лимита
            elif current_length >= absolute_max_chars:
                should_close = True

        if should_close:
            chunk_text = " ".join(str(u.get("text", "") or u.get("transcript", "")).strip() for u in current_batch)
            chunk_dict = {
                "chunk_index": chunk_index,
                "start_sec": float(current_batch[0]["start"]),
                "end_sec": float(current_batch[-1]["end"]),
                "text": chunk_text,
            }
            chunks_meta.append((chunk_dict, closed_by_pause))

            chunk_index += 1
            current_batch = []
            current_length = 0

    # Закрываем последний чанк, если остался хвост
    if current_batch:
        chunk_text = " ".join(str(u.get("text", "") or u.get("transcript", "")).strip() for u in current_batch)
        chunk_dict = {
            "chunk_index": chunk_index,
            "start_sec": float(current_batch[0]["start"]),
            "end_sec": float(current_batch[-1]["end"]),
            "text": chunk_text,
        }
        chunks_meta.append((chunk_dict, True))  # Конец файла считаем естественной паузой

    def split_into_sentences(t: str) -> list[str]:
        # Разделяем по точкам, восклицательным и вопросительным знакам с пробелом
        sentences = re.split(r"(?<=[.?!])\s+", t)
        return [s.strip() for s in sentences if s.strip()]

    final_chunks = [meta[0] for meta in chunks_meta]
    return final_chunks


def batch_texts_for_embedding(texts: list[str]) -> list[list[str]]:
    """Simple chunking for API limits if needed."""
    # Filter empty
    valid_indices = [idx for idx, t in enumerate(texts) if t.strip()]
    if not valid_indices:
        return []

    valid_texts = [texts[idx] for idx in valid_indices]
    return [valid_texts]
