"""Token-aware text chunker for KB documents.

We don't depend on a real tokenizer here — a ~4 chars/token heuristic is close
enough for chunking prose, and avoids pulling tiktoken onto the hot path.
"""

from __future__ import annotations

from typing import List

_CHARS_PER_TOKEN = 4


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def chunk_text(
    text: str,
    target_tokens: int = 500,
    overlap_tokens: int = 80,
) -> List[str]:
    """Split text into roughly-sized chunks with a small overlap.

    Prefers splitting at paragraph boundaries, then sentence boundaries, and
    finally by word count so we never emit a chunk that's wildly off-target.
    Overlap preserves context across boundaries for retrieval.
    """
    if not text or not text.strip():
        return []

    target_chars = target_tokens * _CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * _CHARS_PER_TOKEN

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks: List[str] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            chunks.append(buffer.strip())
            # Seed the next chunk with the tail of this one for overlap.
            buffer = buffer[-overlap_chars:] if overlap_chars else ""
        else:
            buffer = ""

    for para in paragraphs:
        if len(buffer) + len(para) + 2 <= target_chars:
            buffer = f"{buffer}\n\n{para}" if buffer else para
            continue

        # Paragraph doesn't fit — flush then decide.
        if buffer:
            flush()

        if len(para) <= target_chars:
            buffer = f"{buffer}\n\n{para}" if buffer else para
            continue

        # Paragraph too big — split on sentences.
        sentences = _split_sentences(para)
        for sent in sentences:
            if len(buffer) + len(sent) + 1 <= target_chars:
                buffer = f"{buffer} {sent}".strip() if buffer else sent
            else:
                if buffer:
                    flush()
                # If a single sentence exceeds target, hard-split by chars.
                while len(sent) > target_chars:
                    chunks.append(sent[:target_chars])
                    sent = sent[target_chars - overlap_chars:]
                buffer = sent

    if buffer.strip():
        chunks.append(buffer.strip())

    return chunks


def _split_sentences(text: str) -> List[str]:
    """Very light sentence splitter — good enough for chunking."""
    out: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in ".!?" and len(buf) > 20:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def approx_token_count(text: str) -> int:
    return _approx_tokens(text)
