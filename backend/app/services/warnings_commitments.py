"""Phase 4 — Deal Warnings + Commitments extractor.

One focused Haiku pass over the call's analysis JSON + compressed
transcript that produces two structured outputs the rest of the
codebase didn't have before:

1. **Warnings** — finite-vocabulary risk findings (single_threaded,
   competitor_mentioned, etc.) that replace the opaque "risk score"
   chip with named, click-to-expand evidence. The LLM picks from a
   fixed list (see :data:`VALID_WARNING_KINDS`); anything outside the
   set is dropped or coerced to ``other``. Each warning has an
   evidence excerpt + originating interaction.

2. **Commitments** — both-sides promises ("I'll send pricing Friday"
   from the rep, "David will loop in CTO Tuesday" from the customer).
   Distinct from action items (which are rep-side TODOs). Anchored to
   the originating interaction's ``created_at`` so relative phrases
   ("by Friday") remain meaningful when viewed weeks later.

Persistence rules:

* Warnings dedupe on ``(customer_id, kind)``. Re-detection of an
  existing kind clears ``dismissed_at`` and bumps
  ``last_detected_at`` — that way a user-dismissed warning re-raises
  if the underlying signal recurs in a later call. This mirrors how
  the plan describes the "warnings can be re-raised" behavior.
* Commitments are append-only at extraction time. Done-detection is
  a separate, later pass: when a *new* interaction lands on the same
  customer, the same Haiku call is given the open commitments and
  asked which (if any) were satisfied by what was just said. That
  matching pass writes ``status='done'`` + ``completed_via='llm_match'``
  on hits.

Called from ``_run_pipeline_impl`` *after* entity_resolution has run,
so we have ``interaction.customer_id`` populated. Best-effort: the
caller wraps in a try/except so a Haiku flake doesn't fail the whole
pipeline.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from backend.app.models import (
    Commitment,
    Contact,
    CustomerWarning,
    Interaction,
    User,
)
from backend.app.services.llm_client import get_async_anthropic
from backend.app.services.llm_telemetry import record_llm_completion
from backend.app.services.llm_client import model_for_tier

logger = logging.getLogger(__name__)


VALID_WARNING_KINDS = frozenset({
    "single_threaded",
    "champion_silent",
    "competitor_mentioned",
    "no_next_step",
    "exec_disengaged",
    "pricing_unapproved",
    "stalled_renewal",
    "negative_sentiment_trend",
    "other",
})

VALID_SEVERITIES = frozenset({"low", "medium", "high"})

# Weighted-recency window for the "negative sentiment trend" detector.
# Sentiment is per-call, but a trend needs at least two calls;
# warnings_engine reads the customer's analyzed-interactions sentiment
# series and flags when the last 3 are trending down by >0.2 in [0, 1]
# space. Local rule, no LLM cost.
_TREND_WINDOW = 3
_TREND_DELTA_THRESHOLD = 0.2


_PROMPT = (
    "You are an expert call analyst doing two narrow structured-output "
    "tasks on top of an already-completed analysis. Use neutral third "
    "person where prose is required. Return valid JSON only — no "
    "markdown fences, no preamble.\n\n"
    "Return JSON with these exact keys:\n\n"
    "{\n"
    '  "warnings": [\n'
    "    {\n"
    '      "kind": "single_threaded" | "champion_silent" | '
    '"competitor_mentioned" | "no_next_step" | "exec_disengaged" | '
    '"pricing_unapproved" | "stalled_renewal" | '
    '"negative_sentiment_trend" | "other",\n'
    '      "severity": "low" | "medium" | "high",\n'
    '      "evidence_excerpt": str,\n'
    '      "label": str | null   // only required when kind == "other"\n'
    "    }\n"
    "  ],\n"
    '  "commitments": [\n'
    "    {\n"
    '      "actor_side": "rep" | "customer",\n'
    '      "actor_name": str | null,    // person who promised\n'
    '      "target_name": str | null,   // person promised TO (often null)\n'
    '      "text": str,                 // the promise itself\n'
    '      "due_phrase": str | null,    // "by Friday", "EOD today", null\n'
    '      "evidence_excerpt": str\n'
    "    }\n"
    "  ]\n"
    "}\n\n"
    "Hard rules for warnings:\n"
    "- Only emit warnings when the transcript supports them. Empty list "
    "  is the right answer for a healthy call.\n"
    "- ``single_threaded`` only when the rep is talking to one person "
    "  at the customer with no other contacts referenced — not just "
    "  because only one person spoke.\n"
    "- ``competitor_mentioned`` requires a named competitor (Gong, "
    "  Outreach, Salesloft, etc.) being seriously evaluated, not a "
    "  passing mention.\n"
    "- ``pricing_unapproved`` when pricing was discussed but the "
    "  customer hasn't run it past the budget owner / procurement.\n"
    "- ``no_next_step`` when the call ends without a concrete next "
    "  meeting / deliverable / decision date.\n"
    "- ``other`` should be rare; use it only for a real risk that "
    "  doesn't fit any kind above. Provide a short ``label``.\n"
    "- ``severity``: low = worth knowing, medium = worth a follow-up, "
    "  high = needs intervention.\n"
    "- ``evidence_excerpt`` is a short verbatim quote (one or two "
    "  sentences) showing why the warning applies. Must come from the "
    "  transcript.\n\n"
    "Hard rules for commitments:\n"
    "- A commitment is a promise to do a specific thing by a specific "
    "  (or implied) time. Vague intent ('we should circle back') is "
    "  not a commitment.\n"
    "- Both sides count: rep promises and customer promises both belong "
    "  in the list. Use ``actor_side`` to mark which.\n"
    "- ``actor_name`` is the personal name of the speaker who made the "
    "  promise (e.g. 'Maria', 'David Aluko'). Null if the speaker is "
    "  unidentifiable.\n"
    "- ``target_name`` is whoever the promise is to, often null when "
    "  it's the whole-room kind ('I'll send the deck').\n"
    "- ``text`` is the promise stated cleanly in first-person if rep, "
    "  third-person if customer (e.g. rep: 'I'll send pricing by "
    "  Friday'; customer: 'David will review the RFP rubric'). \n"
    "- ``due_phrase`` is the time language verbatim from the call ('by "
    "  Friday', 'EOD today', 'next Tuesday'). Null if no time was given.\n"
    "- ``evidence_excerpt`` is the verbatim sentence containing the "
    "  promise.\n"
)


_DONE_MATCH_PROMPT = (
    "You are an expert call analyst. Below are (a) a list of open "
    "commitments from earlier calls on this customer, and (b) the "
    "transcript of a NEW call. Identify which (if any) of the open "
    "commitments were satisfied by something said in the new call.\n\n"
    "Return JSON only:\n\n"
    "{\n"
    '  "completed": [\n'
    '    {"id": "<uuid of the commitment>", '
    '"evidence_excerpt": "<verbatim quote from new call>"}\n'
    "  ]\n"
    "}\n\n"
    "Hard rules:\n"
    "- Only flag a commitment as completed when the new call provides "
    "  clear evidence the action was done ('I sent the proposal "
    "  yesterday'), not just discussed.\n"
    "- It's fine to return an empty list. Most calls don't close out "
    "  prior commitments.\n"
    "- Never invent commitments; pick only from the provided list.\n"
)


# Time-phrase parser. Matches phrases the LLM emits as ``due_phrase``
# — keep simple and conservative; anything we can't parse falls back
# to ``due_date=NULL`` rather than guessing wrong.
_DAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _parse_due_phrase(
    phrase: Optional[str], anchor: datetime
) -> Optional[datetime]:
    """Resolve "by Friday" / "EOD today" / "next Tuesday" against an anchor.

    Returns ``None`` if the phrase doesn't match a known shape — the
    UI then renders the commitment as "no due date" rather than a
    misleading wrong date. ``anchor`` is the originating interaction's
    ``created_at`` so the parser is reproducible regardless of when
    later calls run.
    """
    if not phrase or not isinstance(phrase, str):
        return None
    p = phrase.strip().lower()
    if not p:
        return None

    # Strip leading prepositions.
    p = re.sub(r"^(by|before|on|due|due by|until)\s+", "", p)
    p = p.strip()

    if p in ("eod", "eod today", "today", "end of day"):
        return _end_of_day(anchor)
    if p in ("eow", "end of week"):
        return _end_of_week(anchor)
    if p == "tomorrow" or p == "by tomorrow":
        return _end_of_day(anchor + timedelta(days=1))
    if p in ("eom", "end of month", "month end"):
        return _end_of_month(anchor)

    # "next Tuesday"
    m = re.match(r"next (\w+)$", p)
    if m and m.group(1) in _DAY_NAMES:
        target = _DAY_NAMES[m.group(1)]
        return _end_of_day(_advance_to_weekday(anchor, target, force_next_week=True))

    # Bare day name — "Friday", "by Friday".
    if p in _DAY_NAMES:
        target = _DAY_NAMES[p]
        return _end_of_day(_advance_to_weekday(anchor, target))

    # "in N days"
    m = re.match(r"in (\d+) days?$", p)
    if m:
        return _end_of_day(anchor + timedelta(days=int(m.group(1))))

    return None


def _end_of_day(d: datetime) -> datetime:
    return d.replace(hour=23, minute=59, second=0, microsecond=0)


def _end_of_week(d: datetime) -> datetime:
    days_to_friday = (4 - d.weekday()) % 7
    if days_to_friday == 0:
        days_to_friday = 0  # already Friday — that's fine
    return _end_of_day(d + timedelta(days=days_to_friday))


def _end_of_month(d: datetime) -> datetime:
    if d.month == 12:
        nxt = d.replace(year=d.year + 1, month=1, day=1)
    else:
        nxt = d.replace(month=d.month + 1, day=1)
    return _end_of_day(nxt - timedelta(days=1))


def _advance_to_weekday(
    d: datetime, target_weekday: int, force_next_week: bool = False
) -> datetime:
    delta = (target_weekday - d.weekday()) % 7
    if delta == 0 and force_next_week:
        delta = 7
    elif delta == 0:
        delta = 7  # "Friday" said on a Friday means next Friday
    return d + timedelta(days=delta)


# ── Dataclasses for parsed LLM output ───────────────────────────────


@dataclass
class WarningExtraction:
    kind: str
    severity: str
    evidence_excerpt: str
    label: Optional[str] = None
    metadata_: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CommitmentExtraction:
    actor_side: str  # rep | customer
    actor_name: Optional[str]
    target_name: Optional[str]
    text: str
    due_phrase: Optional[str]
    evidence_excerpt: str


@dataclass
class ExtractionPayload:
    warnings: List[WarningExtraction] = field(default_factory=list)
    commitments: List[CommitmentExtraction] = field(default_factory=list)


@dataclass
class WarningsCommitmentsOutcome:
    """Returned to the pipeline so it can log + surface counts."""

    warnings_upserted: int = 0
    warnings_re_raised: int = 0
    commitments_created: int = 0
    commitments_marked_done: int = 0
    skipped_no_customer: bool = False


# ── Public entry point ──────────────────────────────────────────────


async def detect_and_persist(
    *,
    session: Session,
    interaction: Interaction,
    tenant: Any,
    insights: Dict[str, Any],
    compressed_transcript: str,
) -> WarningsCommitmentsOutcome:
    """Run extraction, persist warnings + commitments, scan for done.

    Best-effort: any exception is logged and an empty outcome returned
    so the pipeline can continue. Mutates the session with new rows
    but does NOT commit — the caller's existing ``session.commit()``
    flushes the lot.
    """
    outcome = WarningsCommitmentsOutcome()
    customer_id = interaction.customer_id

    if customer_id is None:
        # Without a resolved customer, we can't attach warnings. We can
        # still extract commitments (they have an interaction_id), but
        # they'd be invisible in the UI without a customer. Skip both.
        outcome.skipped_no_customer = True
        return outcome

    payload = await _extract(
        insights=insights, compressed_transcript=compressed_transcript
    )

    # ── Done-detection on prior open commitments ────────────────────
    # Run before persisting new ones so we don't accidentally match a
    # commitment from this same call against itself.
    open_existing = (
        session.query(Commitment)
        .filter(
            Commitment.customer_id == customer_id,
            Commitment.status == "pending",
        )
        .all()
    )
    if open_existing:
        completed = await _scan_done(
            open_commitments=open_existing,
            new_compressed=compressed_transcript,
        )
        for c_id, evidence in completed:
            row = next((c for c in open_existing if c.id == c_id), None)
            if row is None:
                continue
            row.status = "done"
            row.completed_at = _now()
            row.completed_via = "llm_match"
            row.completed_evidence_interaction_id = interaction.id
            outcome.commitments_marked_done += 1

    # ── Warnings: upsert by (customer_id, kind) ─────────────────────
    anchor = interaction.created_at or _now()
    detected_kinds: List[str] = []
    for w in payload.warnings:
        if w.kind not in VALID_WARNING_KINDS:
            continue
        if w.severity not in VALID_SEVERITIES:
            w.severity = "medium"
        detected_kinds.append(w.kind)

        existing = (
            session.query(CustomerWarning)
            .filter(
                CustomerWarning.customer_id == customer_id,
                CustomerWarning.kind == w.kind,
            )
            .first()
        )
        if existing is not None:
            re_raised = existing.dismissed_at is not None
            existing.dismissed_at = None
            existing.dismissed_by = None
            existing.severity = w.severity
            existing.evidence_text = w.evidence_excerpt
            existing.evidence_interaction_id = interaction.id
            existing.last_detected_at = _now()
            meta = dict(existing.metadata_ or {})
            if w.label and w.kind == "other":
                meta["label"] = w.label
            existing.metadata_ = meta
            outcome.warnings_upserted += 1
            if re_raised:
                outcome.warnings_re_raised += 1
        else:
            meta: Dict[str, Any] = {}
            if w.label and w.kind == "other":
                meta["label"] = w.label
            row = CustomerWarning(
                tenant_id=tenant.id,
                customer_id=customer_id,
                kind=w.kind,
                severity=w.severity,
                evidence_text=w.evidence_excerpt,
                evidence_interaction_id=interaction.id,
                metadata_=meta,
            )
            session.add(row)
            outcome.warnings_upserted += 1

    # Local-rule: negative_sentiment_trend (no LLM cost). Computed off
    # the customer's last N analyzed-interaction sentiment scores.
    trend_warning = _compute_sentiment_trend_warning(
        session=session, tenant_id=tenant.id, customer_id=customer_id
    )
    if trend_warning is not None:
        existing = (
            session.query(CustomerWarning)
            .filter(
                CustomerWarning.customer_id == customer_id,
                CustomerWarning.kind == "negative_sentiment_trend",
            )
            .first()
        )
        if existing is not None:
            re_raised = existing.dismissed_at is not None
            existing.dismissed_at = None
            existing.dismissed_by = None
            existing.severity = trend_warning["severity"]
            existing.evidence_text = trend_warning["evidence_text"]
            existing.evidence_interaction_id = interaction.id
            existing.last_detected_at = _now()
            outcome.warnings_upserted += 1
            if re_raised:
                outcome.warnings_re_raised += 1
        else:
            session.add(
                CustomerWarning(
                    tenant_id=tenant.id,
                    customer_id=customer_id,
                    kind="negative_sentiment_trend",
                    severity=trend_warning["severity"],
                    evidence_text=trend_warning["evidence_text"],
                    evidence_interaction_id=interaction.id,
                    metadata_=trend_warning.get("metadata") or {},
                )
            )
            outcome.warnings_upserted += 1
        detected_kinds.append("negative_sentiment_trend")

    # ── Commitments: persist as new rows ────────────────────────────
    for c in payload.commitments:
        side = c.actor_side if c.actor_side in ("rep", "customer") else "unknown"
        actor_user_id, actor_contact_id = _resolve_actor(
            session=session,
            tenant_id=tenant.id,
            customer_id=customer_id,
            side=side,
            name=c.actor_name,
        )
        target_user_id, target_contact_id = _resolve_actor(
            session=session,
            tenant_id=tenant.id,
            customer_id=customer_id,
            side="rep" if side == "customer" else "customer" if side == "rep" else "unknown",
            name=c.target_name,
        )
        due_date = _parse_due_phrase(c.due_phrase, anchor)
        session.add(
            Commitment(
                tenant_id=tenant.id,
                customer_id=customer_id,
                interaction_id=interaction.id,
                actor_user_id=actor_user_id,
                actor_contact_id=actor_contact_id,
                target_user_id=target_user_id,
                target_contact_id=target_contact_id,
                text=c.text,
                evidence_excerpt=c.evidence_excerpt,
                due_date=due_date,
                actor_side=side,
                status="pending",
            )
        )
        outcome.commitments_created += 1

    # Stash a compact debug record on the interaction so the
    # /interactions/{id} endpoint shows what Phase 4 produced. Mirrors
    # the pattern entity_resolution introduced — keeps Phase 4
    # decisions observable without a new endpoint.
    existing_meta = getattr(interaction, "insights", None) or {}
    if isinstance(existing_meta, dict):
        existing_meta = dict(existing_meta)
    else:
        existing_meta = {}
    existing_meta["warnings_commitments_debug"] = {
        "detected_warning_kinds": detected_kinds,
        "warnings_upserted": outcome.warnings_upserted,
        "warnings_re_raised": outcome.warnings_re_raised,
        "commitments_created": outcome.commitments_created,
        "commitments_marked_done": outcome.commitments_marked_done,
    }
    interaction.insights = existing_meta

    return outcome


# ── LLM extraction ──────────────────────────────────────────────────


async def _extract(
    *, insights: Dict[str, Any], compressed_transcript: str
) -> ExtractionPayload:
    client = get_async_anthropic()

    user_block_parts: List[str] = []
    if insights.get("summary"):
        user_block_parts.append(
            f"My one-paragraph summary of the call:\n{insights['summary']}"
        )
    if insights.get("topics"):
        try:
            topics_brief = ", ".join(
                str(t.get("topic") or t) if isinstance(t, dict) else str(t)
                for t in (insights.get("topics") or [])[:8]
            )
            user_block_parts.append(f"Topics surfaced: {topics_brief}")
        except Exception:
            pass
    user_block_parts.append(
        "Compressed transcript:\n" + compressed_transcript[:18_000]
    )
    user_block = "\n\n".join(user_block_parts)

    try:
        resp = await client.messages.create(
            model=model_for_tier("haiku"),
            max_tokens=2000,
            system=[
                {
                    "type": "text",
                    "text": _PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_block}],
        )
    except Exception:
        logger.exception("warnings_commitments: Haiku call failed")
        return ExtractionPayload()

    record_llm_completion("warnings_commitments_extract", "haiku", 2000, resp)

    raw = "".join(
        getattr(b, "text", "") for b in (resp.content or [])
    ).strip()
    raw = _strip_md_fences(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "warnings_commitments: Haiku returned non-JSON: %s", raw[:300]
        )
        return ExtractionPayload()

    return _parse_payload(parsed)


def _parse_payload(payload: Dict[str, Any]) -> ExtractionPayload:
    out = ExtractionPayload()
    for w in payload.get("warnings") or []:
        if not isinstance(w, dict):
            continue
        kind = w.get("kind")
        if not isinstance(kind, str):
            continue
        kind = kind.strip()
        if kind not in VALID_WARNING_KINDS:
            kind = "other"
        sev = w.get("severity")
        sev = sev if isinstance(sev, str) and sev in VALID_SEVERITIES else "medium"
        evid = w.get("evidence_excerpt") or ""
        if not isinstance(evid, str):
            continue
        evid = evid.strip()[:600]
        if not evid:
            continue
        label = w.get("label")
        out.warnings.append(
            WarningExtraction(
                kind=kind,
                severity=sev,
                evidence_excerpt=evid,
                label=label.strip()[:120] if isinstance(label, str) else None,
            )
        )

    for c in payload.get("commitments") or []:
        if not isinstance(c, dict):
            continue
        text_value = c.get("text")
        if not isinstance(text_value, str) or not text_value.strip():
            continue
        side = c.get("actor_side")
        side = side if side in ("rep", "customer") else "unknown"
        evid = c.get("evidence_excerpt") or ""
        evid = evid.strip()[:600] if isinstance(evid, str) else ""
        out.commitments.append(
            CommitmentExtraction(
                actor_side=side,
                actor_name=_clean_name(c.get("actor_name")),
                target_name=_clean_name(c.get("target_name")),
                text=text_value.strip()[:500],
                due_phrase=(
                    c.get("due_phrase").strip()
                    if isinstance(c.get("due_phrase"), str)
                    and c.get("due_phrase").strip()
                    else None
                ),
                evidence_excerpt=evid,
            )
        )
    return out


def _clean_name(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or s.lower() in ("null", "none", "n/a"):
        return None
    return s[:120]


# ── Done-detection (prior commitments matched against new transcript) ──


async def _scan_done(
    *,
    open_commitments: List[Commitment],
    new_compressed: str,
) -> List[Tuple[uuid.UUID, str]]:
    if not open_commitments:
        return []
    client = get_async_anthropic()

    listing = "\n".join(
        f"- id={c.id} text=\"{(c.text or '').strip()[:200]}\""
        for c in open_commitments
    )
    user_block = (
        "Open commitments:\n" + listing
        + "\n\nNew call transcript:\n" + new_compressed[:14_000]
    )
    try:
        resp = await client.messages.create(
            model=model_for_tier("haiku"),
            max_tokens=800,
            system=[
                {
                    "type": "text",
                    "text": _DONE_MATCH_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_block}],
        )
    except Exception:
        logger.exception("warnings_commitments: done-match call failed")
        return []

    record_llm_completion("warnings_commitments_done_match", "haiku", 800, resp)

    raw = "".join(getattr(b, "text", "") for b in (resp.content or [])).strip()
    raw = _strip_md_fences(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "warnings_commitments: done-match returned non-JSON: %s", raw[:300]
        )
        return []

    out: List[Tuple[uuid.UUID, str]] = []
    valid_ids = {c.id for c in open_commitments}
    for entry in parsed.get("completed") or []:
        if not isinstance(entry, dict):
            continue
        try:
            cid = uuid.UUID(str(entry.get("id")))
        except (ValueError, TypeError):
            continue
        if cid not in valid_ids:
            continue
        evid = entry.get("evidence_excerpt") or ""
        if not isinstance(evid, str):
            continue
        out.append((cid, evid.strip()[:400]))
    return out


# ── Sentiment-trend warning (local rule, no LLM) ────────────────────


def _compute_sentiment_trend_warning(
    *, session: Session, tenant_id: uuid.UUID, customer_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    """Detect a downward sentiment trend over recent analyzed calls.

    Reads the customer's last :data:`_TREND_WINDOW` analyzed
    interactions sorted by ``created_at`` and flags when the most
    recent score is at least :data:`_TREND_DELTA_THRESHOLD` below the
    earliest in the window. Cheap, deterministic, no LLM call.
    """
    rows = (
        session.query(Interaction.id, Interaction.insights, Interaction.created_at)
        .filter(
            Interaction.tenant_id == tenant_id,
            Interaction.customer_id == customer_id,
            Interaction.status == "analyzed",
        )
        .order_by(Interaction.created_at.desc())
        .limit(_TREND_WINDOW)
        .all()
    )
    if len(rows) < _TREND_WINDOW:
        return None

    scores: List[float] = []
    for _id, insights, _created in rows:
        if not isinstance(insights, dict):
            continue
        s = insights.get("sentiment_score")
        try:
            scores.append(float(s))
        except (TypeError, ValueError):
            continue
    if len(scores) < _TREND_WINDOW:
        return None

    # ``rows`` is desc-by-created_at; reverse to chronological so we
    # check oldest → newest.
    chrono = list(reversed(scores))
    delta = chrono[0] - chrono[-1]
    if delta < _TREND_DELTA_THRESHOLD:
        return None

    severity = "high" if delta >= 0.4 else "medium" if delta >= 0.3 else "low"
    return {
        "severity": severity,
        "evidence_text": (
            f"Sentiment trended {chrono[0]:.2f} → {chrono[-1]:.2f} over the "
            f"last {_TREND_WINDOW} analyzed calls."
        ),
        "metadata": {"trend": chrono, "delta": round(delta, 3)},
    }


# ── Actor resolution (LLM emits a name, we map it to a User/Contact) ──


def _resolve_actor(
    *,
    session: Session,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    side: str,
    name: Optional[str],
) -> Tuple[Optional[uuid.UUID], Optional[uuid.UUID]]:
    """Map an LLM-emitted name to (user_id, contact_id), at most one set.

    rep-side names map to ``users``; customer-side to ``contacts``
    attached to this customer. Best-effort: if no clean match is
    found, return ``(None, None)`` and the row records the side via
    ``actor_side`` regardless.
    """
    if not name:
        return (None, None)
    n = name.strip().lower()
    if not n:
        return (None, None)

    if side == "rep":
        users = session.query(User).filter(User.tenant_id == tenant_id).all()
        for row in users:
            if _name_matches(row.name, n):
                return (row.id, None)
        return (None, None)

    if side == "customer":
        cs = (
            session.query(Contact)
            .filter(
                Contact.tenant_id == tenant_id,
                Contact.customer_id == customer_id,
            )
            .all()
        )
        for row in cs:
            if _name_matches(row.name, n):
                return (None, row.id)
        return (None, None)

    return (None, None)


def _name_matches(stored: Optional[str], needle_lower: str) -> bool:
    if not stored:
        return False
    full = stored.strip().lower()
    if not full:
        return False
    if full == needle_lower or needle_lower in full or full in needle_lower:
        return True
    # First-name only match — "Maria" should match "Maria Tellez".
    first = full.split()[0] if full.split() else ""
    n_first = needle_lower.split()[0] if needle_lower.split() else ""
    return bool(first) and bool(n_first) and first == n_first


# ── Misc helpers ────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _strip_md_fences(s: str) -> str:
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()
