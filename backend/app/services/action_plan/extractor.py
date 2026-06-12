"""Call D — slot extraction from inbound emails and manual notes.

Plus the RFC 822 inbound-email matcher that ties a freshly-ingested
email to the step whose outbound message it replies to. Both surfaces
land in this module because the matcher's output is what Call D
consumes; keeping them together avoids cross-import gymnastics.

Failure-mode locks honored here:

* Auto-apply with override: extracted values flow into downstream
  artifacts immediately. The agent's later edits set
  ``agent_overridden=True`` so the audit trail captures who changed
  what.
* Ambiguous inbound: match strictly via In-Reply-To + References
  chain. When no outbound message id matches, leave the inbound
  unattached and surface it as "needs manual routing" (the engine's
  query layer reads this state).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import ActionStep, StepResponse
from backend.app.services.action_plan.prompts import CALL_D_SYSTEM_PROMPT
from backend.app.services.llm_client import get_async_anthropic
from backend.app.services.triage_service import _strip_json_fences
from backend.app.services.llm_client import model_for_tier

logger = logging.getLogger(__name__)


_EXTRACTION_MODEL = model_for_tier("haiku")
_EXTRACTION_MAX_TOKENS = 2048


# ──────────────────────────────────────────────────────────
# Inbound email matcher (RFC 822)
# ──────────────────────────────────────────────────────────


@dataclass
class MatchResult:
    """Outcome of matching one inbound email to a step.

    ``step_id`` is None when no match was found (the inbound is
    surfaced to the agent for manual routing) OR when the inbound
    matched but the step is already closed (no-op).
    """

    step_id: Optional[uuid.UUID]
    outbound_message_id: Optional[str]
    reason: str  # 'in_reply_to' | 'references' | 'no_match' | 'step_closed'


async def match_inbound_email(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    in_reply_to: Optional[str],
    references: Sequence[str],
) -> MatchResult:
    """Find the open step whose outbound this inbound replies to.

    Lookup order:
    1. Direct In-Reply-To match against ``step_responses.outbound_message_id``
       where source='outbound_email_sent'.
    2. Walk the References chain (most recent first) for a match.
    3. No match: return ``MatchResult(step_id=None, reason='no_match')``.

    Per the locked decision, we never guess - if neither header matches
    a known outbound message id, the inbound is left unattached and
    surfaced to the agent.
    """
    candidate_ids = []
    if in_reply_to:
        candidate_ids.append((in_reply_to, "in_reply_to"))
    for ref in (references or [])[::-1]:  # most recent end of chain first
        if ref and ref != in_reply_to:
            candidate_ids.append((ref, "references"))

    if not candidate_ids:
        return MatchResult(
            step_id=None, outbound_message_id=None, reason="no_match",
        )

    # Query step_responses for any outbound_message_id matching any
    # candidate. We pull all matches and pick the most recent so that
    # if the same outbound was somehow logged twice we still attach
    # to the live step.
    msg_ids = [cid for cid, _ in candidate_ids]
    rows = await db.execute(
        select(StepResponse).where(
            StepResponse.tenant_id == tenant_id,
            StepResponse.outbound_message_id.in_(msg_ids),
            StepResponse.source == "outbound_email_sent",
        )
        .order_by(StepResponse.received_at.desc())
    )
    matches = list(rows.scalars())
    if not matches:
        return MatchResult(
            step_id=None, outbound_message_id=None, reason="no_match",
        )

    # Pick the one whose outbound id appears earliest in candidate_ids
    # (In-Reply-To beats References) — guaranteed by stable order.
    chosen: Optional[StepResponse] = None
    chosen_reason = "no_match"
    for msg_id, reason in candidate_ids:
        for m in matches:
            if m.outbound_message_id == msg_id:
                chosen = m
                chosen_reason = reason
                break
        if chosen is not None:
            break
    if chosen is None:
        return MatchResult(
            step_id=None, outbound_message_id=None, reason="no_match",
        )

    # Verify the step is still open. A done/skipped/deleted step
    # ignores the inbound (the engine surfaces it as informational on
    # the closed step rather than re-opening).
    step = await db.get(ActionStep, chosen.step_id)
    if step is None or step.state in {"done", "skipped", "deleted"}:
        return MatchResult(
            step_id=chosen.step_id,
            outbound_message_id=chosen.outbound_message_id,
            reason="step_closed",
        )

    return MatchResult(
        step_id=chosen.step_id,
        outbound_message_id=chosen.outbound_message_id,
        reason=chosen_reason,
    )


# Strip quoted reply chains from inbound email bodies so Call D only
# sees the new content. Catches the common patterns Gmail / Outlook
# insert; doesn't try to be exhaustive (the prompt also tells the
# model to ignore quoted history as a safety net).
_QUOTE_LINE_RE = re.compile(r"^\s*>")
_REPLY_HEADER_PATTERNS = [
    re.compile(r"^On\s.+\swrote:\s*$", re.IGNORECASE),
    re.compile(r"^From:\s.+", re.IGNORECASE),
    re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^_{3,}", re.IGNORECASE),
]


def strip_quoted_reply(body: str) -> str:
    """Return the body with quoted history trimmed off the end.

    Conservative — when in doubt we keep the content (a false positive
    that strips real new content is worse than the model briefly
    seeing a quoted line it should ignore).
    """
    if not body:
        return ""
    lines = body.splitlines()
    cut_at = len(lines)
    in_quote = False
    consecutive_quote_lines = 0
    for i, line in enumerate(lines):
        for pat in _REPLY_HEADER_PATTERNS:
            if pat.search(line):
                cut_at = i
                in_quote = True
                break
        if in_quote:
            break
        if _QUOTE_LINE_RE.match(line):
            consecutive_quote_lines += 1
            if consecutive_quote_lines >= 3:
                # Three consecutive quoted lines is almost always the
                # start of quoted history.
                cut_at = i - 2
                break
        else:
            consecutive_quote_lines = 0
    trimmed = "\n".join(lines[:cut_at]).rstrip()
    return trimmed or body  # never return empty when input was non-empty


# ──────────────────────────────────────────────────────────
# Call D — extraction
# ──────────────────────────────────────────────────────────


@dataclass
class ExtractionResult:
    extracted: Dict[str, Any]
    source_quotes: Dict[str, str]
    unfilled_reasons: Dict[str, str]
    confidence: float


class ResponseExtractor:
    """Pulls structured slot values out of an inbound email or note."""

    def __init__(
        self,
        client: Optional[anthropic.AsyncAnthropic] = None,
    ) -> None:
        self._client = client or get_async_anthropic()

    async def extract_for_step(
        self,
        *,
        step: ActionStep,
        source_label: str,  # 'inbound email' | 'manual note'
        source_content: str,
    ) -> ExtractionResult:
        """Run Call D against ``source_content`` for ``step``."""
        if not source_content or not source_content.strip():
            return ExtractionResult({}, {}, {}, 0.0)

        if source_label == "inbound email":
            source_content = strip_quoted_reply(source_content)

        schema = _format_output_schema(step.output_schema)
        system_prompt = CALL_D_SYSTEM_PROMPT.format(
            source_label=source_label,
            step_title=step.title,
            step_intent=step.intent or step.description or "",
            output_schema_block=schema,
            source_content=source_content[:8000],
        )
        try:
            response = await self._client.messages.create(
                model=_EXTRACTION_MODEL,
                max_tokens=_EXTRACTION_MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        # The system prompt varies per step (step title +
                        # intent + schema embedded), so caching has limited
                        # hit-rate; we mark ephemeral anyway in case a
                        # batch of notes for the same step lands together.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Extract the slot values now per the schema "
                            "in the system prompt. Return ONLY JSON."
                        ),
                    }
                ],
            )
            raw_text = response.content[0].text
            data = json.loads(_strip_json_fences(raw_text))
        except (
            anthropic.APIError,
            json.JSONDecodeError,
            IndexError,
            KeyError,
            AttributeError,
        ) as exc:
            logger.warning(
                "Call D extraction failed for step %s: %s", step.id, exc,
            )
            return ExtractionResult({}, {}, {}, 0.0)

        if not isinstance(data, dict):
            return ExtractionResult({}, {}, {}, 0.0)

        extracted = data.get("extracted") if isinstance(data.get("extracted"), dict) else {}
        quotes = data.get("source_quotes") if isinstance(data.get("source_quotes"), dict) else {}
        reasons = data.get("unfilled_reasons") if isinstance(data.get("unfilled_reasons"), dict) else {}
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        # Restrict to declared slot_keys so the model can't smuggle in
        # extra fields that would muddy downstream input_slots wiring.
        allowed_keys = {
            s.get("slot_key")
            for s in (step.output_schema or [])
            if isinstance(s, dict)
        }
        extracted = {k: v for k, v in extracted.items() if k in allowed_keys}
        quotes = {
            k: str(v)[:300]
            for k, v in quotes.items()
            if k in allowed_keys and v is not None
        }
        reasons = {
            k: str(v)[:300]
            for k, v in reasons.items()
            if k in allowed_keys and v is not None
        }

        return ExtractionResult(
            extracted=extracted,
            source_quotes=quotes,
            unfilled_reasons=reasons,
            confidence=confidence,
        )


def _format_output_schema(schema: Any) -> str:
    if not isinstance(schema, list) or not schema:
        return "(no output_schema declared - return extracted={} with reason)"
    lines: List[str] = []
    for s in schema:
        if not isinstance(s, dict):
            continue
        lines.append(
            f"- {s.get('slot_key')} ({s.get('type', 'string')}): "
            f"{s.get('description', '')}"
        )
    return "\n".join(lines)


__all__ = [
    "ResponseExtractor",
    "ExtractionResult",
    "MatchResult",
    "match_inbound_email",
    "strip_quoted_reply",
]
