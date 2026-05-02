import pytest
from app.transcription.postprocessing import apply_text_replacements, apply_postprocessing_to_raw

def test_apply_text_replacements():
    assert apply_text_replacements("я мастер") == "я Мастер"
    assert apply_text_replacements("мастеру привет") == "Мастеру привет"
    assert apply_text_replacements("мастера") == "Мастера"
    assert apply_text_replacements("немастер") == "немастер" # No boundary

def test_apply_postprocessing_to_raw():
    raw = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "мастер сказал",
                            "words": [{"word": "мастер"}, {"punctuated_word": "мастер."}]
                        }
                    ]
                }
            ],
            "utterances": [
                {"transcript": "привет мастер", "words": [{"word": "мастер"}]}
            ]
        }
    }
    processed = apply_postprocessing_to_raw(raw)
    
    alt = processed["results"]["channels"][0]["alternatives"][0]
    assert alt["transcript"] == "Мастер сказал"
    assert alt["words"][0]["word"] == "Мастер"
    assert alt["words"][1]["punctuated_word"] == "Мастер."
    
    utt = processed["results"]["utterances"][0]
    assert utt["transcript"] == "привет Мастер"
    assert utt["words"][0]["word"] == "Мастер"
