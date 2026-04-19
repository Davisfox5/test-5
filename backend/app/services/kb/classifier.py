"""Hybrid question/intent classifier for caller turns.

Two paths:

* **Fast path (regex)** — catches the explicit majority (sub-millisecond, free).
  When Deepgram's streaming keyterm prompting reports a hit, that arrives here
  as a flag and we short-circuit "is_question=True".
* **Slow path (Haiku)** — only invoked when the fast path is uncertain; handles
  implicit questions like "I'm not sure about X".

Deepgram pricing check (verified April 2026): keyterm prompting is an add-on
at ~$0.0013/min, and streaming intent/topic detection is not offered. The
Haiku savings from keyterms would be smaller than the keyterm surcharge in the
average case, so keyterms are **tenant-opt-in only** (set ``question_keyterms``
on the tenant). Default classifier is regex + Haiku fallback.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import anthropic

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

# Obvious question/request starters. Case-insensitive, word-boundary anchored.
_QUESTION_RE = re.compile(
    r"\b(how|what|when|where|why|which|who|whose|whom|can|could|would|should|"
    r"do|does|did|is|are|was|were|will|may|might|have|has|had)\b"
    r"|\?",
    re.IGNORECASE,
)

# Implicit-question / objection markers worth flagging.
_OBJECTION_RE = re.compile(
    r"\b(not sure|unclear|don't (understand|know)|confused|hesitant|worried "
    r"about|concerned about|problem with|issue with|too expensive|too much|"
    r"tell me more|explain)\b",
    re.IGNORECASE,
)


@dataclass
class ClassifierResult:
    is_question: bool
    query: str
    urgency: str  # "high" | "normal"
    source: str  # "regex" | "haiku" | "deepgram_keyterm" | "skipped"


async def classify(
    caller_text: str,
    *,
    deepgram_keyterm_hit: bool = False,
    use_haiku_fallback: bool = True,
) -> ClassifierResult:
    """Decide whether ``caller_text`` merits a KB lookup.

    Args:
        caller_text: The caller's (not agent's) most recent final transcript.
        deepgram_keyterm_hit: True if Deepgram flagged a tenant-configured
            question keyterm in this turn.
        use_haiku_fallback: When False, skip the Haiku call on ambiguous turns
            (used in tests and when rate-limiting Haiku spend).
    """
    text = (caller_text or "").strip()
    if not text:
        return ClassifierResult(False, "", "normal", "skipped")

    # 1. Deepgram keyterm hit — strongest explicit signal.
    if deepgram_keyterm_hit:
        return ClassifierResult(True, text, "high", "deepgram_keyterm")

    # 2. Regex — fast and free.
    if _QUESTION_RE.search(text):
        urgency = "high" if text.endswith("?") else "normal"
        return ClassifierResult(True, text, urgency, "regex")

    # 3. Haiku — only for ambiguous turns that weren't caught above but might
    #    still be an implicit question ("I'm not sure about pricing").
    if not use_haiku_fallback:
        return ClassifierResult(False, text, "normal", "skipped")

    if _OBJECTION_RE.search(text):
        return await _haiku_classify(text)

    return ClassifierResult(False, text, "normal", "skipped")


async def _haiku_classify(text: str) -> ClassifierResult:
    settings = get_settings()
    try:
        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        resp = await client.messages.create(
            model=_CLASSIFIER_MODEL,
            max_tokens=120,
            system=(
                "You classify a single caller turn from a sales/support call.\n"
                "Decide whether the caller is implicitly asking for information "
                "the agent could answer from a knowledge base.\n"
                "Respond with JSON only, no prose:\n"
                '{"is_question": bool, "query": "...", "urgency": "high|normal"}\n'
                "- If yes, set query to a concise restatement of what they want "
                "answered (<=15 words).\n"
                "- urgency=high if they sound blocked, urgency=normal otherwise."
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text
        data = json.loads(raw)
        return ClassifierResult(
            bool(data.get("is_question", False)),
            str(data.get("query", text))[:300],
            str(data.get("urgency", "normal")),
            "haiku",
        )
    except (anthropic.APIError, json.JSONDecodeError, KeyError, IndexError):
        logger.exception("Haiku classifier fell back to skip")
        return ClassifierResult(False, text, "normal", "skipped")
