from app.chunking import batch_texts_for_embedding, chunk_from_utterances


def test_chunk_from_utterances_empty():
    assert chunk_from_utterances([]) == []


def test_chunk_from_utterances_single():
    utterances = [{"start": 0, "end": 10, "text": "Hello world", "speaker": 0}]
    chunks = chunk_from_utterances(utterances, max_chars=100)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Hello world"
    assert chunks[0]["start_sec"] == 0
    assert chunks[0]["end_sec"] == 10
    assert chunks[0]["speaker"] == "0"


def test_chunk_from_utterances_grouping():
    utterances = [
        {"start": 0, "end": 5, "text": "Short", "speaker": 1},
        {"start": 6, "end": 10, "text": "Text", "speaker": 1},
        {"start": 11, "end": 15, "text": "Combined", "speaker": 2},
    ]
    # max_chars=10 should group at least first two
    chunks = chunk_from_utterances(utterances, max_chars=10)
    assert len(chunks) >= 1
    # Depending on implementation detail (>= max_chars),
    # current code closes chunk AFTER reaching max_chars.
    # "Short" (5) < 10 -> continue
    # "Short" + "Text" (9) < 10 -> continue
    # "Short" + "Text" + "Combined" (17) >= 10 -> close.
    # So it groups all 3 if max_chars is 100.

    chunks_small = chunk_from_utterances(utterances, max_chars=5)
    # "Short" (5) >= 5 -> close chunk 1
    # "Text" (4) < 5 -> continue
    # "Text" + "Combined" (12) >= 5 -> close chunk 2
    assert len(chunks_small) == 2
    assert chunks_small[0]["text"] == "Short"
    assert chunks_small[1]["text"] == "Text Combined"
    assert chunks_small[1]["speaker"] == "1, 2"


def test_batch_texts_for_embedding():
    texts = ["hello", " ", "world", ""]
    batches = batch_texts_for_embedding(texts)
    assert len(batches) == 1
    assert batches[0] == ["hello", "world"]


def test_batch_texts_for_embedding_empty():
    assert batch_texts_for_embedding(["", "  "]) == []
