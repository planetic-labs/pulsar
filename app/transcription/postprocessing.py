import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Dictionary of replacements: {pattern: replacement}
# Using \b for word boundaries and capturing Russian suffixes
REPLACEMENTS = {
    r"\bмастер([ауеы]|ом|ам|ами|ах)?\b": r"Мастер\1",
}


def apply_text_replacements(text: str) -> str:
    """Applies all registered regex replacements to a string."""
    if not text:
        return text

    for pattern, replacement in REPLACEMENTS.items():
        text = re.sub(pattern, replacement, text)
    return text


def apply_postprocessing_to_raw(raw_payload: dict[str, Any]) -> dict[str, Any]:
    """
    Surgically updates text fields in the Deepgram raw response.
    Targeting:
    - results.channels[].alternatives[].transcript
    - results.channels[].alternatives[].words[].word
    - results.channels[].alternatives[].words[].punctuated_word
    - results.utterances[].transcript
    - results.utterances[].words[].word
    - results.utterances[].words[].punctuated_word
    """
    results = raw_payload.get("results", {})

    # 1. Update channels/alternatives
    for channel in results.get("channels", []):
        for alt in channel.get("alternatives", []):
            if "transcript" in alt:
                alt["transcript"] = apply_text_replacements(alt["transcript"])

            # Deepgram also has individual words with their own confidence/timestamps
            if "words" in alt:
                for word_info in alt["words"]:
                    if "word" in word_info:
                        word_info["word"] = apply_text_replacements(word_info["word"])
                    if "punctuated_word" in word_info:
                        word_info["punctuated_word"] = apply_text_replacements(word_info["punctuated_word"])

            # Paragraphs (if paragraphs=true was used)
            if "paragraphs" in alt:
                paragraphs_data = alt["paragraphs"].get("paragraphs", [])
                for p in paragraphs_data:
                    for sentence in p.get("sentences", []):
                        if "text" in sentence:
                            sentence["text"] = apply_text_replacements(sentence["text"])

    # 2. Update utterances (if diarize=true was used)
    for utterance in results.get("utterances", []):
        if "transcript" in utterance:
            utterance["transcript"] = apply_text_replacements(utterance["transcript"])
        if "words" in utterance:
            for word_info in utterance["words"]:
                if "word" in word_info:
                    word_info["word"] = apply_text_replacements(word_info["word"])
                if "punctuated_word" in word_info:
                    word_info["punctuated_word"] = apply_text_replacements(word_info["punctuated_word"])

    return raw_payload
