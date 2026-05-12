import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Dictionary of replacements: {pattern: replacement_base}
# Using \b for word boundaries and capturing Russian suffixes
# The replacement_base is the capitalized version of the word root.
REPLACEMENTS = {
    r"\bмастер([ауеы]|ом|ам|ами|ах)?\b": "Мастер",
    r"\bдисциплин([аыеу]|ой|ою)?\b": "Дисциплин",
    r"\bпросветлени([еяюи]|ем)?\b": "Просветлени",
    r"\bистин([аыеу]|ой|ою)?\b": "Истин",
}


def apply_text_replacements(text: str) -> str:
    """Applies all registered regex replacements to a string."""
    if not text:
        return text

    for pattern, base in REPLACEMENTS.items():

        def repl(match: re.Match, base_val: str = base) -> str:
            suffix = match.group(1) or ""
            return base_val + suffix.lower()

        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
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
                p_obj = alt["paragraphs"]
                if "transcript" in p_obj:
                    p_obj["transcript"] = apply_text_replacements(p_obj["transcript"])

                paragraphs_data = p_obj.get("paragraphs", [])
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
