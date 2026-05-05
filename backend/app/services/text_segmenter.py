"""Speaker-tag parser for text-ingested transcripts.

The text-ingest path used to wrap ``raw_text`` in a single segment with
``start=0``, ``end=0``, ``speaker_id=None`` — which made the
interaction-detail page render every call as one wall-of-paragraph
attributed to "Speaker 1" at 0:00. This parser splits the text into
proper turns when it can identify speaker labels.

Recognized shapes:

  REP: Hi David, thanks for making time today.
  PROSPECT: No problem, Maria.
  Maria Chen: That's a fair concern.
  CSM: ...

Anything before the first speaker-tagged line (title block, "Date:",
"Participants:" header) is treated as preamble and dropped from the
rendered transcript. If the parser finds NO speaker tags it falls
back to a single segment matching the legacy behavior — so any
unstructured text, real Deepgram output, or random pasted notes
still works.

When timestamps are missing (text ingest never has them), turns are
distributed evenly across the interaction's ``duration_seconds`` so
the SPA's scrub bar still renders something sensible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


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


def segments_from_text(
    raw_text: Optional[str],
    *,
    duration_seconds: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Build pipeline-ready segments from a text transcript.

    Returns a list of dicts shaped like the rest of the pipeline expects
    (``start``, ``end``, ``text``, ``speaker_id``, ``confidence``). When
    speaker-tag parsing finds nothing usable, falls back to a single
    segment so downstream code keeps the same contract.

    ``duration_seconds`` (optional) is used to distribute synthetic
    timestamps evenly across the call when the source didn't carry
    them. Without it, all turns get ``start=0``, ``end=0`` — same as
    the legacy single-segment behavior, just split by speaker.
    """
    if not raw_text:
        return []

    turns = parse_speaker_tagged(raw_text)
    if not turns:
        # Legacy single-segment fallback so nothing weirder than the
        # old behavior happens for inputs we can't parse.
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
) -> Tuple[Dict[str, int], Dict[str, str]]:
    """Map raw labels → stable integer speaker ids + display names.

    Display name is the original label (REP / Maria Chen / CSM) — the
    extractor's later entity-resolution step does the heavy lifting on
    mapping these back to canonical Users / Contacts.
    """
    label_to_id: Dict[str, int] = {}
    label_to_speaker: Dict[str, str] = {}
    for t in turns:
        if t.label not in label_to_id:
            label_to_id[t.label] = len(label_to_id)
            label_to_speaker[t.label] = t.label
    return label_to_id, label_to_speaker
