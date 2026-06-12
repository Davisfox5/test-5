"""Speaker-tag parser for text-ingested transcripts.

Canonical first step of the text-ingest pipeline. Every downstream
analyzer (AI analysis prompt, call_metrics, rapport LSM, inline tags)
consumes speaker-tagged segments, so if this step produces a single
blob the rest of the pipeline silently degrades.

Two-stage strategy:

1. **Pattern parser** (``parse_speaker_tagged``) — recognizes explicit
   speaker tags like ``REP:``, ``CSM:``, ``Maria Chen:``. Fast, free,
   zero LLM dependency. Works for any source that already tags
   speakers (Gong/Chorus/Otter exports, in-house transcripts, our
   synthetic seed data).
2. **LLM fallback** (``parse_via_llm``) — fires when the pattern
   parser yields < 2 distinct speakers. Sends the raw text to Haiku
   with instructions to rewrite it as ``REP: ...`` / ``CUSTOMER: ...``
   lines, then re-runs the pattern parser on the labeled output.
   Used for third-party transcription services that emit
   un-diarized text (most do, today), or for sources like AIxBlock's
   call-center corpus where speakers are implicit.

If both stages fail, we fall back to the legacy single-segment so
downstream code still gets a contract-shaped output — but most
analyzers will degrade. The fallback should be rare in production.

When timestamps are missing (text ingest never has them), turns are
distributed evenly across the interaction's ``duration_seconds`` so
the SPA's scrub bar still renders something sensible.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from backend.app.services.llm_client import model_for_tier

logger = logging.getLogger(__name__)


# A line is a speaker turn when it starts (column 0) with one of:
#   - An ALL-CAPS role tag of 1-4 words: ``REP:``, ``CSM:``, ``VP SALES:``
#   - A capitalized personal name (1-3 words, optional middle initial):
#     ``David Aluko:``, ``Maria Chen:``, ``Allison J. Park:``
# We require the colon to be at the very start of the trailing space — so
# prose like "I told them: this won't work" is NOT treated as a speaker
# tag (no leading ``\n`` before it, no all-caps / personal-name shape).
_SPEAKER_LINE = re.compile(
    r"""
    ^                                # start of line
    (?P<label>
        (?:[A-Z][A-Z0-9_]{1,15}(?:\s[A-Z][A-Z0-9_]{1,15}){0,3})  # ROLE / VP SALES
        |
        (?:[A-Z][a-z]+(?:\s[A-Z]\.)?(?:\s[A-Z][a-z]+){0,2})       # Personal Name
    )
    \s*:\s+                          # colon + at least one trailing space
    (?P<text>.+)$                    # rest of line is the turn text
    """,
    re.MULTILINE | re.VERBOSE,
)


# Lines we always skip in preamble parsing — these are metadata headers
# that occasionally use a colon-shape ("Date:", "Channel:") and would
# otherwise be detected as a speaker tag.
_METADATA_PREFIXES = (
    "date",
    "duration",
    "channel",
    "participants",
    "agenda",
    "attendees",
    "meeting",
    "subject",
    "from",
    "to",
    "cc",
    "title",
)


@dataclass
class ParsedTurn:
    label: str  # raw label from the line — "REP", "Maria Chen", "CSM"
    text: str


def parse_speaker_tagged(raw_text: str) -> List[ParsedTurn]:
    """Pull turns out of a text transcript.

    Returns an empty list when no speaker tags are found — caller decides
    the fallback (legacy single-segment, etc.).
    """
    if not raw_text or not raw_text.strip():
        return []

    turns: List[ParsedTurn] = []
    current_label: Optional[str] = None
    current_buf: List[str] = []

    def flush() -> None:
        if current_label is not None and current_buf:
            text = " ".join(s.strip() for s in current_buf).strip()
            if text:
                turns.append(ParsedTurn(label=current_label, text=text))

    for raw_line in raw_text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        # Metadata header lines — skip while we're still in preamble
        # (i.e. before the first speaker tag). Once we've started
        # collecting turns, blank lines + metadata-style lines become
        # part of the current turn (rare but possible).
        if current_label is None:
            head = line.split(":", 1)[0].strip().lower()
            if head in _METADATA_PREFIXES:
                continue

        m = _SPEAKER_LINE.match(line)
        if m:
            flush()
            current_label = m.group("label").strip()
            current_buf = [m.group("text").strip()]
        elif current_label is not None:
            # Continuation of the current turn (line wrap or paragraph
            # break inside the same speaker's content).
            current_buf.append(line.strip())
        # else: still in preamble — drop until we hit the first tag.

    flush()
    return turns


def _distinct_labels(turns: List[ParsedTurn]) -> int:
    return len({t.label for t in turns})


def parse_via_llm(raw_text: str) -> List[ParsedTurn]:
    """Use Haiku to tag speakers in un-diarized text.

    Sends the raw transcript to Claude Haiku with instructions to emit
    a re-formatted version where every utterance is prefixed with
    ``REP:`` or ``CUSTOMER:``. We then re-run the deterministic
    pattern parser on Haiku's output — this keeps the contract narrow
    (Haiku can't hallucinate a 3-speaker call into existence) and
    reuses the existing speaker-id assignment logic.

    Synchronous + blocking — meant to be called from the worker, not
    the API. Returns ``[]`` on any failure so the caller can fall
    back to the legacy single-segment behavior.
    """
    if not raw_text or len(raw_text.strip()) < 80:
        # Too short to be worth an LLM call.
        return []
    try:
        import anthropic
        from backend.app.config import get_settings
    except Exception:
        return []

    settings = get_settings()
    api_key = getattr(settings, "ANTHROPIC_API_KEY", None)
    if not api_key:
        return []

    system_prompt = (
        "You are a transcript formatter. Add speaker labels to the call "
        "transcript below.\n\n"
        "Rules:\n"
        "1. Use exactly two labels: ``REP:`` for the call-center agent / "
        "salesperson / rep, and ``CUSTOMER:`` for the person being called "
        "or who called in.\n"
        "2. Put EACH utterance on its own line, prefixed with the label "
        "and a single space (no trailing punctuation in the label).\n"
        "3. Do NOT add, remove, paraphrase, or summarize words. Preserve "
        "the verbatim text. Just split it into turns and label them.\n"
        "4. Identify turn boundaries by question-then-response shape, "
        "topic shifts, or natural conversational rhythm.\n"
        "5. The first speaker is almost always the REP for outbound "
        "calls and the CUSTOMER for inbound calls. Use call content to "
        "decide; if unclear, default to REP-first.\n"
        "6. Output ONLY the labeled transcript. No preamble, no notes, "
        "no markdown fences."
    )

    try:
        client = anthropic.Anthropic(
            api_key=api_key,
            timeout=getattr(settings, "ANTHROPIC_TIMEOUT_SECONDS", 60),
        )
        # Cap input + output to keep this cheap. Haiku at $0.25/M in +
        # $1.25/M out — even a 10K-token transcript costs < $0.02 per
        # call to segment. Worth it to unblock downstream analysis.
        resp = client.messages.create(
            model=model_for_tier("haiku"),
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": raw_text}],
        )
        from backend.app.services.llm_telemetry import record_llm_completion

        record_llm_completion("text_segmenter", "haiku", 8192, resp)
        labeled = resp.content[0].text if resp.content else ""
    except Exception:
        logger.exception("LLM speaker-tagging failed; falling back")
        return []

    turns = parse_speaker_tagged(labeled)
    if _distinct_labels(turns) < 2:
        # Haiku produced output but it didn't yield 2+ speakers — the
        # input probably wasn't actually a two-party conversation.
        return []
    return turns


def segments_from_text(
    raw_text: Optional[str],
    *,
    duration_seconds: Optional[float] = None,
    use_llm_fallback: bool = True,
) -> List[Dict[str, Any]]:
    """Build pipeline-ready segments from a text transcript.

    Two-stage extraction:

    1. Pattern parser — picks up any explicit ``REP:`` / ``CSM:`` /
       ``Maria Chen:`` tags. Fast path.
    2. LLM fallback — when the pattern parser produces fewer than 2
       distinct speakers AND ``use_llm_fallback`` is true, Haiku adds
       labels and we re-parse.

    Returns a list of dicts shaped ``{start, end, text, speaker_id,
    speaker, speaker_label, confidence}``. When both stages fail, falls
    back to a single-segment with ``speaker_id=None`` so downstream
    code still gets a contract-shaped output — but most analyzers
    will degrade silently. The fallback should be rare in production.

    ``duration_seconds`` (optional) is used to distribute synthetic
    timestamps evenly across the call when the source didn't carry
    them. Without it, turns get ``start=0``, ``end=0``.
    """
    if not raw_text:
        return []

    # Stage 1: pattern parser.
    turns = parse_speaker_tagged(raw_text)

    # Stage 2: LLM fallback when the pattern parser didn't find a real
    # two-party conversation. Disabled in tests via ``use_llm_fallback``.
    if use_llm_fallback and _distinct_labels(turns) < 2:
        llm_turns = parse_via_llm(raw_text)
        if llm_turns:
            logger.info(
                "Speaker segmentation: LLM fallback produced %d turns "
                "across %d speakers (pattern parser yielded %d turns)",
                len(llm_turns),
                _distinct_labels(llm_turns),
                len(turns),
            )
            turns = llm_turns

    if not turns:
        # Legacy single-segment fallback — kept so anything truly
        # unstructured still flows through the pipeline.
        return [
            {
                "start": 0.0,
                "end": float(duration_seconds or 0.0),
                "text": raw_text.strip(),
                "speaker_id": None,
                "confidence": None,
            }
        ]

    label_to_id, label_to_speaker = _assign_speaker_ids(turns)

    n = len(turns)
    if duration_seconds and duration_seconds > 0 and n > 0:
        per = float(duration_seconds) / n
        starts = [round(i * per, 2) for i in range(n)]
        ends = [round((i + 1) * per, 2) for i in range(n)]
    else:
        starts = [0.0] * n
        ends = [0.0] * n

    out: List[Dict[str, Any]] = []
    for i, t in enumerate(turns):
        out.append(
            {
                "start": starts[i],
                "end": ends[i],
                "text": t.text,
                "speaker_id": label_to_id[t.label],
                "speaker": label_to_speaker[t.label],
                "speaker_label": t.label,
                "confidence": None,
            }
        )
    return out


def _assign_speaker_ids(
    turns: List[ParsedTurn],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Map raw labels → stable string speaker ids + display names.

    Returns STRING ids ("0", "1", …) to match Deepgram's convention so
    every downstream consumer (call_metrics, rapport, inline_tags) can
    treat speaker_id uniformly. The earlier integer scheme triggered a
    Python falsy bug in call_metrics (``sid = seg.speaker_id or
    "__unknown__"`` collapses ``0`` to the fallback) which mis-bucketed
    every REP segment as unknown.

    Display name is the original label (REP / Maria Chen / CSM).
    """
    label_to_id: Dict[str, str] = {}
    label_to_speaker: Dict[str, str] = {}
    for t in turns:
        if t.label not in label_to_id:
            label_to_id[t.label] = str(len(label_to_id))
            label_to_speaker[t.label] = t.label
    return label_to_id, label_to_speaker
