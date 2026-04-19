"""CustomerBriefBuilder — per-customer dossier agent.

For each Customer (the tenant's CRM account), we maintain a living brief in
``customers.customer_brief``. LINDA's live-coaching and analysis agents load
it at call time so they can ground hints in what we know about this specific
account — their history, preferences, open risks, and what's historically
worked with them.

Triggered:

* On interaction close (debounced, like the tenant brief).
* On admin demand via ``POST /customers/{id}/brief/rebuild``.
* Weekly sweep alongside the tenant refiner (optional).

Inputs:

* All the customer's contacts + their interaction_count / sentiment_trend.
* Recent ``Interaction.insights`` for calls with any of those contacts
  (sentiment, churn/upsell signals, coaching, action_items, key_moments,
  competitor mentions, product_feedback).
* ``CustomerOutcomeEvent`` rows for the customer (lifecycle events).

Output (stored in ``customers.customer_brief``):

* ``current_status`` — active | at_risk | churning | champion | new | dormant
* ``overview`` — short identity + what they bought / are evaluating
* ``stakeholders`` — [{name, role, preferences}]
* ``interests`` — topics they've engaged with
* ``objections_raised`` — [{objection, context, resolved}]
* ``preferences`` — tone/pacing/channel notes observed over time
* ``best_approaches`` — what's historically worked with them
* ``avoid`` — what hasn't worked
* ``churn_signals`` / ``upsell_signals`` — aggregated
* ``timeline`` — compact chronology of the last N significant moments
* ``updated_at`` / ``source_interaction_count``
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import (
    Contact,
    Customer,
    CustomerNote,
    CustomerOutcomeEvent,
    Interaction,
)

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_INTERACTIONS = 40
_MAX_SUMMARY_CHARS = 500


_SYSTEM_PROMPT = (
    "You are the CustomerBriefBuilder agent for LINDA. You read the history "
    "of one customer account — their people, their interactions, their "
    "outcomes, and any agent-authored notes — and produce a compact living "
    "dossier that LINDA's other agents consult at call time.\n\n"
    "Respond in JSON only (no markdown fences) with this shape:\n"
    "{\n"
    '  "current_status": "active|at_risk|churning|champion|new|dormant",\n'
    '  "overview": "2-3 sentences: who they are, what stage, what we sold/are selling",\n'
    '  "stakeholders": [{"name": "...", "role": "...", "preferences": "..."}],\n'
    '  "interests": ["topics they care about"],\n'
    '  "objections_raised": [{"objection": "...", "context": "...", "resolved": bool}],\n'
    '  "preferences": "tone, pacing, channel observations",\n'
    '  "best_approaches": ["what has worked on this account"],\n'
    '  "avoid": ["what has NOT worked"],\n'
    '  "churn_signals": ["active risk indicators"],\n'
    '  "upsell_signals": ["active expansion indicators"],\n'
    '  "timeline": [{"when": "ISO date", "note": "one-line moment"}],\n'
    '  "field_confidences": {\n'
    '    "overview": 0.0-1.0,\n'
    '    "stakeholders": 0.0-1.0,\n'
    '    "interests": 0.0-1.0,\n'
    '    "objections_raised": 0.0-1.0,\n'
    '    "preferences": 0.0-1.0,\n'
    '    "best_approaches": 0.0-1.0,\n'
    '    "avoid": 0.0-1.0,\n'
    '    "churn_signals": 0.0-1.0,\n'
    '    "upsell_signals": 0.0-1.0\n'
    "  }\n"
    "}\n\n"
    "Rules:\n"
    "- Keep the whole brief under ~500 words.\n"
    "- Ground every field in the provided evidence — do not invent.\n"
    "- field_confidences[x] should reflect how strongly the evidence backs "
    "field x; 0.9+ for repeatedly-confirmed observations, 0.5-0.7 for one "
    "signal, <0.4 only when you're making a stretch inference. Leave missing "
    "when the field is empty.\n"
    "- Weight agent notes heavily — they're explicit human observations.\n"
    "- If a field has no evidence, use empty string or empty array.\n"
    "- Prefer recent signals over old ones when they conflict.\n"
    "- ``current_status`` decision guide: ``churning`` if a churned/at_risk "
    "event is in the last 60 days; ``at_risk`` if churn_signal high in last "
    "30; ``champion`` if upsell_signal high or advocate_signal event; "
    "``new`` if this is the first interaction; ``dormant`` if no interaction "
    "in 60+ days; otherwise ``active``."
)


class CustomerBriefBuilder:
    """Builds/refreshes one customer's brief."""

    def __init__(self, client: Optional[anthropic.AsyncAnthropic] = None) -> None:
        if client is not None:
            self._client = client
        else:
            settings = get_settings()
            self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def build(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> Dict[str, Any]:
        customer = await db.get(Customer, customer_id)
        if customer is None or customer.tenant_id != tenant_id:
            raise ValueError(f"Customer {customer_id} not found for tenant")

        contacts = list(
            (
                await db.execute(
                    select(Contact).where(
                        Contact.tenant_id == tenant_id,
                        Contact.customer_id == customer_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        contact_ids = [c.id for c in contacts]

        interactions: List[Interaction] = []
        if contact_ids:
            interactions = list(
                (
                    await db.execute(
                        select(Interaction)
                        .where(
                            Interaction.tenant_id == tenant_id,
                            Interaction.contact_id.in_(contact_ids),
                        )
                        .order_by(Interaction.created_at.desc())
                        .limit(_MAX_INTERACTIONS)
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
                        CustomerOutcomeEvent.customer_id == customer_id,
                    )
                    .order_by(CustomerOutcomeEvent.detected_at.desc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )

        notes = list(
            (
                await db.execute(
                    select(CustomerNote)
                    .where(
                        CustomerNote.tenant_id == tenant_id,
                        CustomerNote.customer_id == customer_id,
                    )
                    .order_by(CustomerNote.created_at.desc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )

        evidence = _build_evidence(customer, contacts, interactions, events, notes)

        if not interactions and not events and not notes:
            brief = _empty_brief()
            brief["current_status"] = "new"
            brief["field_confidences"] = {}
            return await self._persist(
                db, customer, brief, interaction_count=0, notes=notes
            )

        brief = await self._call_haiku(evidence)
        return await self._persist(
            db, customer, brief, interaction_count=len(interactions), notes=notes
        )

    async def _call_haiku(self, evidence: Dict[str, Any]) -> Dict[str, Any]:
        notes_block = "\n---\n".join(evidence.get("notes") or [])
        user_message = (
            "## Customer\n"
            f"{json.dumps(evidence['customer'])}\n\n"
            "## Contacts\n"
            f"{json.dumps(evidence['contacts'])}\n\n"
            "## Recent interactions (most recent first)\n"
            + "\n---\n".join(evidence["interaction_blocks"])
            + "\n\n## Customer lifecycle events\n"
            + json.dumps(evidence["events"])
            + (
                f"\n\n## Agent notes on this customer\n{notes_block}"
                if notes_block
                else ""
            )
            + "\n\nReturn the brief as JSON."
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
            logger.exception("CustomerBriefBuilder Haiku call failed")
            return _empty_brief()
        return _validate_brief(data)

    @staticmethod
    async def _persist(
        db: AsyncSession,
        customer: Customer,
        brief: Dict[str, Any],
        interaction_count: int,
        notes: Optional[List[CustomerNote]] = None,
    ) -> Dict[str, Any]:
        brief = dict(brief)
        brief["updated_at"] = datetime.now(timezone.utc).isoformat()
        brief["source_interaction_count"] = interaction_count

        # Mark the notes we just ingested as reviewed so the next run won't
        # pretend they're fresh evidence.
        reviewed_at = datetime.now(timezone.utc)
        for note in notes or []:
            if note.reviewed_at is None:
                note.reviewed_at = reviewed_at

        customer.customer_brief = brief

        # Fan out a ``customer_brief.updated`` webhook so external systems
        # (CRM, ops dashboards, Slack bots) can mirror the dossier.
        try:
            from backend.app.services.webhook_dispatcher import emit_event

            await emit_event(
                db,
                customer.tenant_id,
                "customer_brief.updated",
                {
                    "customer_id": str(customer.id),
                    "current_status": brief.get("current_status"),
                    "source_interaction_count": interaction_count,
                    "notes_reviewed": len(notes or []),
                },
            )
        except Exception:
            logger.debug("customer_brief.updated emission failed", exc_info=True)

        return brief


# ───── Helpers ───────────────────────────────────────────────


def _build_evidence(
    customer: Customer,
    contacts: List[Contact],
    interactions: List[Interaction],
    events: List[CustomerOutcomeEvent],
    notes: Optional[List["CustomerNote"]] = None,
) -> Dict[str, Any]:
    return {
        "customer": {
            "id": str(customer.id),
            "name": customer.name,
            "industry": customer.industry,
            "domain": customer.domain,
            "metadata": customer.metadata_ or {},
        },
        "contacts": [
            {
                "name": c.name,
                "email": c.email,
                "interaction_count": c.interaction_count or 0,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "sentiment_trend": (c.sentiment_trend or [])[-10:],
            }
            for c in contacts
        ],
        "interaction_blocks": [_interaction_block(i) for i in interactions],
        "events": [
            {
                "event_type": e.event_type,
                "reason": (e.reason or "")[:200],
                "magnitude": e.magnitude,
                "signal_strength": e.signal_strength,
                "detected_at": e.detected_at.isoformat() if e.detected_at else None,
                "source": e.source,
            }
            for e in events
        ],
        "notes": [
            (
                f"[{n.created_at.isoformat() if n.created_at else ''}"
                f"{' NEW' if n.reviewed_at is None else ''}] {n.body[:500]}"
            )
            for n in (notes or [])
        ],
    }


def _interaction_block(i: Interaction) -> str:
    ins = i.insights or {}
    coaching = ins.get("coaching") or {}
    blob = (
        f"[{i.created_at.date() if i.created_at else ''}] "
        f"{i.channel} / outcome={i.outcome_type}\n"
        f"sentiment={ins.get('sentiment_overall')} "
        f"churn={ins.get('churn_risk_signal')} "
        f"upsell={ins.get('upsell_signal')}\n"
        f"summary: {(ins.get('summary') or '')[:_MAX_SUMMARY_CHARS]}\n"
        f"went_well: {', '.join(str(x) for x in (coaching.get('what_went_well') or [])[:3])}\n"
        f"improvements: {', '.join(str(x) for x in (coaching.get('improvements') or [])[:3])}"
    )
    objections = ins.get("competitor_mentions") or []
    if objections:
        obj_s = "; ".join(
            f"{o.get('name')}={'handled' if o.get('handled_well') else 'missed'}"
            for o in objections
            if isinstance(o, dict)
        )
        blob += f"\nobjections: {obj_s}"
    return blob


def _empty_brief() -> Dict[str, Any]:
    # ``current_status`` is left empty here — the builder fills it in from
    # evidence (e.g., "new" when there are no prior interactions). This lets
    # ``format_customer_brief_for_prompt({})`` and ``format_customer_brief_for_prompt(_empty_brief())``
    # both return "" without injecting placeholder text into the prompt.
    return {
        "current_status": "",
        "overview": "",
        "stakeholders": [],
        "interests": [],
        "objections_raised": [],
        "preferences": "",
        "best_approaches": [],
        "avoid": [],
        "churn_signals": [],
        "upsell_signals": [],
        "timeline": [],
        # Per-field confidences (0.0-1.0) from the last builder run. The
        # frontend reads these to render a badge next to each section.
        "field_confidences": {},
    }


def _validate_brief(data: Dict[str, Any]) -> Dict[str, Any]:
    out = _empty_brief()
    if not isinstance(data, dict):
        return out
    for key, default in out.items():
        val = data.get(key, default)
        if isinstance(default, list):
            out[key] = list(val)[:12] if isinstance(val, list) else default
        elif isinstance(default, dict):
            if isinstance(val, dict):
                clean: Dict[str, Any] = {}
                for k, v in val.items():
                    try:
                        clean[str(k)] = max(0.0, min(1.0, float(v)))
                    except (TypeError, ValueError):
                        continue
                out[key] = clean
            else:
                out[key] = default
        elif isinstance(default, str):
            out[key] = str(val)[:2000] if val is not None else default
    return out


def format_customer_brief_for_prompt(brief: Dict[str, Any]) -> str:
    """Render a customer brief as a compact text block for LINDA's prompts."""
    if not brief:
        return ""

    lines: List[str] = []
    status = brief.get("current_status") or ""
    overview = brief.get("overview") or ""
    if status or overview:
        header = f"**Status: {status}.** " if status else ""
        lines.append(f"{header}{overview}")

    def _bullets(label: str, values, lim: int = 5) -> None:
        if values:
            lines.append(f"**{label}:**")
            for v in values[:lim]:
                lines.append(f"- {v}")

    stakeholders = brief.get("stakeholders") or []
    if stakeholders:
        lines.append("**Stakeholders:**")
        for s in stakeholders[:5]:
            if isinstance(s, dict):
                parts = [s.get("name", ""), s.get("role", ""), s.get("preferences", "")]
                lines.append("- " + " — ".join(p for p in parts if p))

    _bullets("Interests", brief.get("interests"))
    _bullets("Best approaches (what's worked)", brief.get("best_approaches"))
    _bullets("Avoid (what hasn't worked)", brief.get("avoid"))
    _bullets("Active churn signals", brief.get("churn_signals"))
    _bullets("Active upsell signals", brief.get("upsell_signals"))

    objections = brief.get("objections_raised") or []
    if objections:
        lines.append("**Objections raised by this customer:**")
        for o in objections[:5]:
            if isinstance(o, dict):
                mark = "✓" if o.get("resolved") else "✗"
                lines.append(f"- {mark} {o.get('objection', '')} — {o.get('context', '')}")

    preferences = brief.get("preferences")
    if preferences:
        lines.append(f"**Preferences:** {preferences}")

    if not lines:
        return ""
    return "# Customer context\n" + "\n".join(lines)
