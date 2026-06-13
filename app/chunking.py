import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# Дефолтные настройки для экспериментов из переменных окружения
DEFAULT_PAUSE_THRESHOLD = float(os.getenv("CHUNKING_PAUSE_THRESHOLD", "3.0"))
DEFAULT_MIN_CHARS = int(os.getenv("CHUNKING_MIN_CHARS", "400"))
DEFAULT_MAX_CHARS = int(os.getenv("CHUNKING_MAX_CHARS", "800"))
DEFAULT_OVERLAP_SENTENCES = int(os.getenv("CHUNKING_OVERLAP_SENTENCES", "1"))


def chunk_from_utterances(
    utterances: list[dict[str, Any]],
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
    absolute_max_chars: int = 2000,
    pause_threshold: float = DEFAULT_PAUSE_THRESHOLD,
    overlap_sentences: int = DEFAULT_OVERLAP_SENTENCES,
    single_chunk: bool = False,
) -> list[dict[str, Any]]:
    """
    Группирует мелкие реплики (utterances) в семантические чанки на основе:
    1. Накопленного размера текста (не менее min_chars).
    2. Границ предложений (знаки препинания ., ?, !).
    3. Пауз между репликами (пауза >= pause_threshold секунд).

    Если чанк закрывается не по паузе, делает перекрытие на указанное число предложений
    из соседних чанков для сохранения контекста.
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
            except (ValueError, TypeError, KeyError):
                pause = 0.0

            is_long_pause = pause >= pause_threshold

            # Закрываем, если закончили предложение или встретили длинную паузу
            if is_long_pause:
                should_close = True
                closed_by_pause = True
            elif ends_sentence:
                should_close = True

            # Принудительное закрытие при достижении абсолютного лимита
            if current_length >= absolute_max_chars:
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

    num_chunks = len(chunks_meta)

    # Кэшируем оригинальные тексты базовых чанков во избежание сайд-эффектов мутации словарей
    base_texts = [str(meta[0]["text"]) for meta in chunks_meta]

    final_chunks = []

    for idx in range(num_chunks):
        chunk, closed_by_pause = chunks_meta[idx]

        if num_chunks <= 1 or overlap_sentences <= 0:
            final_chunks.append(chunk)
            continue

        prefix_parts = []
        suffix_parts = []

        # 2. Добавляем суффикс из следующего чанка, если текущий чанк закрылся не по паузе
        if idx < num_chunks - 1:
            if not closed_by_pause:
                suffix_parts = split_into_sentences(base_texts[idx + 1])[:overlap_sentences]

        # Собираем финальный текст
        text_components = []
        if prefix_parts:
            text_components.append(" ".join(prefix_parts))
        text_components.append(str(chunk["text"]))
        if suffix_parts:
            text_components.append(" ".join(suffix_parts))

        chunk["text"] = " ".join(text_components)
        final_chunks.append(chunk)

    return final_chunks


def batch_texts_for_embedding(texts: list[str]) -> list[list[str]]:
    """Simple chunking for API limits if needed."""
    # Filter empty
    valid_indices = [idx for idx, t in enumerate(texts) if t.strip()]
    if not valid_indices:
        return []

    valid_texts = [texts[idx] for idx in valid_indices]
    return [valid_texts]
