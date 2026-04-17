"""Transcript compressor — strips filler words and stutters for LLM input.

The compressed output is used *only* as context sent to the LLM for
summarisation / coaching.  The raw transcript is always stored in the DB.
"""

from __future__ import annotations

import re
from typing import List, Optional

from backend.app.services.transcription import Segment

# Filler words / phrases to strip.  Order matters: multi-word phrases are
# removed first so their individual tokens don't get partially matched later.
_FILLER_PHRASES: List[str] = [
    "you know",
    "i mean",
]
_FILLER_WORDS: List[str] = [
    "um", "uh", "like", "so", "basically", "actually", "right",
]

# Pre-compiled patterns (case-insensitive, whole-word).
_PHRASE_PATTERNS: List[re.Pattern] = [  # type: ignore[type-arg]
    re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE)
    for p in _FILLER_PHRASES
]
_WORD_PATTERNS: List[re.Pattern] = [  # type: ignore[type-arg]
    re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE)
    for w in _FILLER_WORDS
]

# Stutter pattern: repeated words like "I I I think" → "I think".
# Matches 2+ consecutive identical words (case-insensitive).
_STUTTER_RE = re.compile(r"\b(\w+)((?:\s+\1)+)\b", re.IGNORECASE)

# Dead-air / silence markers that some transcription engines may insert.
_DEAD_AIR_RE = re.compile(
    r"\[(?:silence|pause|dead air|inaudible)\]",
    re.IGNORECASE,
)

# Collapse multiple spaces.
_MULTI_SPACE_RE = re.compile(r"  +")


class TranscriptCompressor:
    """Strips filler words, stutters, and dead-air markers from segments."""

    def compress(self, segments: List[Segment]) -> List[Segment]:
        """Return a new list of compressed segments.

        - Filler words and phrases are removed.
        - Stutters (``"I I I think"``) are collapsed (``"I think"``).
        - Dead-air markers (``[silence]``, ``[pause]``, etc.) are removed.
        - Speaker IDs, timestamps, and confidence scores are preserved.
        - Segments whose text becomes empty after compression are dropped.
        """
        compressed: List[Segment] = []
        for seg in segments:
            cleaned = self._clean_text(seg.text)
            if not cleaned:
                continue
            compressed.append(
                Segment(
                    start=seg.start,
                    end=seg.end,
                    text=cleaned,
                    speaker_id=seg.speaker_id,
                    confidence=seg.confidence,
                )
            )
        return compressed

    # ── Internal ─────────────────────────────────────────────────────────

    @staticmethod
    def _clean_text(text: str) -> str:
        """Apply all cleaning rules to a single text string."""
        result = text

        # 1. Remove dead-air markers.
        result = _DEAD_AIR_RE.sub("", result)

        # 2. Remove multi-word filler phrases first.
        for pat in _PHRASE_PATTERNS:
            result = pat.sub("", result)

        # 3. Remove single filler words.
        for pat in _WORD_PATTERNS:
            result = pat.sub("", result)

        # 4. Collapse stutters ("the the the idea" → "the idea").
        result = _STUTTER_RE.sub(r"\1", result)

        # 5. Clean up whitespace.
        result = _MULTI_SPACE_RE.sub(" ", result).strip()

        # 6. Fix orphaned punctuation that may result from removals
        # e.g. " , " or leading commas.
        result = re.sub(r"\s+([,.])", r"\1", result)
        result = re.sub(r"^[,.\s]+", "", result)

        return result
