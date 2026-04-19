"""Infer-From-Sources agent — proposes updates to the onboarding-owned
sections of the tenant brief based on what it observes in live data.

Runs periodically (weekly by default, alongside the TenantBriefRefiner).
Emits ``TenantBriefSuggestion`` rows — it **never auto-writes** to the
tenant brief. An admin approves or rejects each suggestion.

Sources it mines:

* Recent ``Interaction.insights`` — coaching notes, product feedback,
  sentiment trajectories, win/loss summaries.
* ``CustomerOutcomeEvent`` rows — escalations, churns, upsells.
* The tenant's existing ``tenant_context.playbook_insights`` —
  ``what_works`` bullets are strong candidates for ``strategies``.
* The tenant's current ``tenant_context`` — we skip suggestions for
  fields the tenant has already set (respects their authority).

Outputs are scoped to onboarding-owned sections:

* ``goals``
* ``kpis``
* ``strategies``
* ``org_structure`` (teams / escalation_path / territories)
* ``personal_touches`` (greeting_style / signoff_style / phrasing_preferences
  / rituals / humor_level / pacing_style / avoid_phrases / etc.)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import (
    CustomerOutcomeEvent,
    Interaction,
    Tenant,
    TenantBriefSuggestion,
)
from backend.app.services.kb.context_builder import _validate_brief

logger = logging.getLogger(__name__)


_MODEL = "claude-haiku-4-5-20251001"
_WINDOW_DAYS = 30
_MAX_INTERACTION_BLOCKS = 20

_ONBOARDING_SECTIONS = {"goals", "kpis", "strategies", "org_structure", "personal_touches"}


_SYSTEM_PROMPT = (
    "You are the Infer-From-Sources agent for LINDA. You read recent call "
    "evidence and propose updates to a tenant's onboarding-owned brief.\n\n"
    "Onboarding-owned sections:\n"
    "* goals — list of strings\n"
    "* kpis — list of {name, target, current?}\n"
    "* strategies — list of strings\n"
    "* org_structure — {teams, escalation_path, territories}\n"
    "* personal_touches — {greeting_style, signoff_style, phrasing_preferences\n"
    "   [{say, dont_say, context}], rituals, humor_level, pacing_style,\n"
    "   empathy_markers, celebration_markers, avoid_phrases, "
    "signature_tagline}\n\n"
    "You will receive:\n"
    "* The tenant's CURRENT onboarding fields (so you don't re-propose what "
    "they already have).\n"
    "* Aggregated evidence from recent interactions: outcome counts, top "
    "phrases observed in wins, common failure modes, escalation patterns.\n"
    "* The tenant's existing playbook_insights (learned section).\n\n"
    "Respond with JSON only (no markdown fences). An array of suggestion "
    "objects:\n"
    "{\n"
    '  "suggestions": [\n'
    "    {\n"
    '      "section": "goals|kpis|strategies|org_structure|personal_touches",\n'
    '      "path": "optional dotted path e.g. personal_touches.greeting_style",\n'
    '      "proposed_value": <the value to add/set>,\n'
    '      "rationale": "why, with reference to evidence",\n'
    '      "confidence": 0.0-1.0\n'
    "    }\n"
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    "- Do NOT propose values that duplicate what's already in the current "
    "fields.\n"
    "- Prefer small, specific, verifiable suggestions over broad ones.\n"
    "- Cap at 8 suggestions. Skip low-confidence (<0.4) ideas entirely.\n"
    "- If the evidence is thin, return an empty array — we'll try again next "
    "cycle."
)


# ── Entry point ────────────────────────────────────────────────────────


@dataclass
class SuggestionOut:
    section: str
    path: Optional[str]
    proposed_value: Any
    rationale: str
    confidence: float
    evidence_refs: List[str]


class InferFromSources:
    """Proposes tenant-brief updates from observed signals."""

    def __init__(self, client: Optional[anthropic.AsyncAnthropic] = None) -> None:
        if client is not None:
            self._client = client
        else:
            settings = get_settings()
            self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def run(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        window_days: int = _WINDOW_DAYS,
    ) -> List[TenantBriefSuggestion]:
        """Mine sources + write ``TenantBriefSuggestion`` rows. Returns the
        newly-created rows. Safe to run repeatedly — duplicates of existing
        pending suggestions are skipped."""

        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found")

        current = _validate_brief(tenant.tenant_context or {})
        playbook = current.get("playbook_insights") or {}

        since = datetime.now(timezone.utc) - timedelta(days=window_days)
        interactions = list(
            (
                await db.execute(
                    select(Interaction)
                    .where(
                        Interaction.tenant_id == tenant_id,
                        Interaction.created_at >= since,
                    )
                    .order_by(Interaction.created_at.desc())
                    .limit(_MAX_INTERACTION_BLOCKS)
                )
            )
            .scalars()
            .all()
        )
        events = list(
            (
                await db.execute(
                    select(CustomerOutcomeEvent)
                    .where(
                        CustomerOutcomeEvent.tenant_id == tenant_id,
                        CustomerOutcomeEvent.detected_at >= since,
                    )
                    .order_by(CustomerOutcomeEvent.detected_at.desc())
                )
            )
            .scalars()
            .all()
        )

        evidence = _build_evidence(current, playbook, interactions, events)
        if evidence["total_interactions"] == 0 and not playbook:
            return []

        raw_suggestions = await self._call_haiku(evidence)
        suggestions = _coerce_suggestions(
            raw_suggestions,
            evidence_refs={
                "interaction_ids": [str(i.id) for i in interactions[:10]],
                "event_ids": [str(e.id) for e in events[:10]],
            },
        )

        # Skip suggestions that duplicate anything we already have in the
        # onboarding-owned sections (exact-value match on strings/list items).
        kept = [s for s in suggestions if not _is_redundant(s, current)]

        # Avoid duplicating pending suggestions already in the DB.
        existing_pending = list(
            (
                await db.execute(
                    select(TenantBriefSuggestion).where(
                        TenantBriefSuggestion.tenant_id == tenant_id,
                        TenantBriefSuggestion.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_keys = {
            _suggestion_key(s.section, s.path, s.proposed_value)
            for s in existing_pending
        }

        new_rows: List[TenantBriefSuggestion] = []
        for s in kept:
            key = _suggestion_key(s.section, s.path, s.proposed_value)
            if key in existing_keys:
                continue
            row = TenantBriefSuggestion(
                tenant_id=tenant_id,
                section=s.section,
                path=s.path,
                proposed_value=s.proposed_value,
                rationale=s.rationale,
                confidence=s.confidence,
                evidence_refs=s.evidence_refs,
                status="pending",
            )
            db.add(row)
            new_rows.append(row)

        if new_rows:
            await db.flush()
        return new_rows

    async def _call_haiku(self, evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
        user_message = (
            "## Current onboarding fields (do not duplicate)\n"
            f"{json.dumps(evidence['current_fields'])}\n\n"
            "## Playbook insights (learned section)\n"
            f"{json.dumps(evidence['playbook'])}\n\n"
            "## Recent activity summary\n"
            f"{json.dumps(evidence['activity_summary'])}\n\n"
            "## Sampled interaction snippets\n"
            + "\n---\n".join(evidence["snippets"])
            + "\n\nReturn suggestions JSON."
        )
        try:
            resp = await self._client.messages.create(
                model=_MODEL,
                max_tokens=1400,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw = resp.content[0].text
            data = json.loads(raw)
        except (anthropic.APIError, json.JSONDecodeError, IndexError, KeyError):
            logger.exception("InferFromSources Haiku call failed")
            return []
        suggestions = data.get("suggestions") if isinstance(data, dict) else None
        return suggestions if isinstance(suggestions, list) else []


# ── Apply / reject helpers ─────────────────────────────────────────────


async def apply_suggestion(
    db: AsyncSession,
    suggestion: TenantBriefSuggestion,
    reviewed_by_user_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Splice an approved suggestion into the tenant's brief."""
    tenant = await db.get(Tenant, suggestion.tenant_id)
    if tenant is None:
        raise ValueError("Tenant gone")

    brief = _validate_brief(tenant.tenant_context or {})
    section = suggestion.section
    if section not in _ONBOARDING_SECTIONS:
        raise ValueError(f"Section {section} is not onboarding-owned")

    value = suggestion.proposed_value
    path = suggestion.path

    if path and "." in path:
        # Nested path for personal_touches / org_structure updates.
        head, rest = path.split(".", 1)
        if head != section:
            raise ValueError(f"Path head {head!r} must match section {section!r}")
        container = dict(brief.get(section) or {})
        container[rest] = value
        brief[section] = container
    elif section in ("goals", "strategies"):
        existing = list(brief.get(section) or [])
        if isinstance(value, list):
            for v in value:
                if v not in existing:
                    existing.append(v)
        elif value and value not in existing:
            existing.append(value)
        brief[section] = existing
    elif section == "kpis":
        existing = list(brief.get(section) or [])
        incoming = value if isinstance(value, list) else [value]
        known_names = {k.get("name") for k in existing if isinstance(k, dict)}
        for k in incoming:
            if isinstance(k, dict) and k.get("name") not in known_names:
                existing.append(k)
        brief[section] = existing
    elif section in ("org_structure", "personal_touches"):
        container = dict(brief.get(section) or {})
        if isinstance(value, dict):
            container.update(value)
        brief[section] = container
    else:
        brief[section] = value

    tenant.tenant_context = brief

    suggestion.status = "approved"
    suggestion.reviewed_at = datetime.now(timezone.utc)
    suggestion.reviewed_by_user_id = reviewed_by_user_id
    return brief


async def reject_suggestion(
    db: AsyncSession,
    suggestion: TenantBriefSuggestion,
    reviewed_by_user_id: Optional[uuid.UUID] = None,
) -> None:
    suggestion.status = "rejected"
    suggestion.reviewed_at = datetime.now(timezone.utc)
    suggestion.reviewed_by_user_id = reviewed_by_user_id


# ── Internal helpers ──────────────────────────────────────────────────


def _build_evidence(
    current: Dict[str, Any],
    playbook: Dict[str, Any],
    interactions: List[Interaction],
    events: List[CustomerOutcomeEvent],
) -> Dict[str, Any]:
    from collections import Counter

    outcome_counts: Counter = Counter(
        i.outcome_type for i in interactions if i.outcome_type
    )
    event_counts: Counter = Counter(e.event_type for e in events)

    current_fields = {k: current.get(k) for k in _ONBOARDING_SECTIONS}

    snippets: List[str] = []
    for i in interactions[:8]:
        ins = i.insights or {}
        coaching = ins.get("coaching") or {}
        snippets.append(
            f"[{i.outcome_type}] {(ins.get('summary') or '')[:250]} | "
            f"went_well={(coaching.get('what_went_well') or [])[:2]} | "
            f"improvements={(coaching.get('improvements') or [])[:2]}"
        )

    return {
        "current_fields": current_fields,
        "playbook": playbook,
        "activity_summary": {
            "outcome_counts": dict(outcome_counts),
            "event_counts": dict(event_counts),
            "total_interactions": len(interactions),
            "total_events": len(events),
        },
        "snippets": snippets,
        "total_interactions": len(interactions),
    }


def _coerce_suggestions(
    raw: List[Dict[str, Any]],
    evidence_refs: Dict[str, List[str]],
) -> List[SuggestionOut]:
    flattened_refs = [
        *(f"interaction:{i}" for i in evidence_refs.get("interaction_ids") or []),
        *(f"event:{e}" for e in evidence_refs.get("event_ids") or []),
    ]
    out: List[SuggestionOut] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        section = str(item.get("section", "")).strip()
        if section not in _ONBOARDING_SECTIONS:
            continue
        confidence = item.get("confidence")
        try:
            conf = float(confidence) if confidence is not None else 0.4
        except (TypeError, ValueError):
            conf = 0.4
        if conf < 0.4:
            continue
        value = item.get("proposed_value")
        if value in (None, "", [], {}):
            continue
        out.append(
            SuggestionOut(
                section=section,
                path=item.get("path"),
                proposed_value=value,
                rationale=str(item.get("rationale", ""))[:800],
                confidence=conf,
                evidence_refs=flattened_refs,
            )
        )
    return out


def _is_redundant(suggestion: SuggestionOut, current: Dict[str, Any]) -> bool:
    """Return True if ``suggestion`` is already covered by the current fields.
    Exact-match on strings/list items; dict merges are only redundant when
    the proposed key is already non-empty with the same value."""
    section = suggestion.section
    value = suggestion.proposed_value
    path = suggestion.path

    if path and "." in path:
        _, rest = path.split(".", 1)
        container = current.get(section) or {}
        return container.get(rest) == value

    if section in ("goals", "strategies"):
        existing = current.get(section) or []
        if isinstance(value, list):
            return all(v in existing for v in value)
        return value in existing
    if section == "kpis":
        existing_names = {
            k.get("name") for k in (current.get(section) or []) if isinstance(k, dict)
        }
        incoming = value if isinstance(value, list) else [value]
        return all(
            isinstance(k, dict) and k.get("name") in existing_names for k in incoming
        )
    if section in ("org_structure", "personal_touches"):
        container = current.get(section) or {}
        if isinstance(value, dict):
            return all(container.get(k) == v for k, v in value.items())
        return False
    return False


def _suggestion_key(section: str, path: Optional[str], value: Any) -> str:
    """Stable key used to dedupe against pending rows."""
    return json.dumps(
        {"section": section, "path": path or "", "value": value},
        sort_keys=True,
        default=str,
    )
