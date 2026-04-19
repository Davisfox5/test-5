"""Unit tests for the KB text chunker."""

from backend.app.services.kb.chunker import chunk_text


def test_empty_text_returns_empty_list():
    assert chunk_text("") == []
    assert chunk_text("   \n  \n") == []


def test_short_text_is_single_chunk():
    out = chunk_text("Hello world.", target_tokens=500)
    assert out == ["Hello world."]


def test_long_text_splits_into_multiple_chunks():
    paragraph = "This is a sample paragraph about sales pricing. " * 100
    text = (paragraph + "\n\n") * 5
    chunks = chunk_text(text, target_tokens=200, overlap_tokens=40)
    assert len(chunks) > 1
    # No chunk should be wildly over target (target_tokens * 4 chars/token, plus
    # a little slack for paragraph boundaries).
    for c in chunks:
        assert len(c) < 200 * 4 * 2


def test_chunks_preserve_content():
    text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
    chunks = chunk_text(text, target_tokens=500)
    joined = " ".join(chunks)
    for needle in ("Paragraph one.", "Paragraph two.", "Paragraph three."):
        assert needle in joined
