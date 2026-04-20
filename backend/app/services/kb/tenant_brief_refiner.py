"""TenantBriefRefiner — the outcomes-loop agent for LINDA's tenant brief.

Once a week (and on admin demand) this agent reads every interaction the
tenant has closed in the window, pulls ``insights`` + ``outcome_type`` +
``coaching`` + any customer-level events, and asks Claude Haiku to identify
what's actually working and what isn't. The result is written into
``tenant_context.playbook_insights`` — a section the ContextBuilder (KB agent)
and onboarding endpoint both leave alone.

Inputs fed to the model:

* Aggregated win/loss counts by ``outcome_type``.
* Sampled snippets from high-scoring calls (``script_adherence_score``).
* Sampled snippets from low-scoring calls.
* Objection → handler patterns from won vs lost calls.
* Churn / upsell signal patterns.

Outputs (merged into ``tenant_context.playbook_insights``):

* ``what_works`` / ``what_doesnt`` — short bullets, max 5 each.
* ``top_performing_phrases`` / ``common_failure_modes``.
* ``winning_objection_handlers`` — [{"objection", "handler"}].
* ``last_learned_at`` / ``sample_size``.
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import CustomerOutcomeEvent, Interaction, Tenant

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_SNIPPET_CHARS = 1200
_MAX_WON_SAMPLES = 6
_MAX_LOST_SAMPLES = 6
_DEFAULT_WINDOW_DAYS = 14

_SYSTEM_PROMPT = (
    "You are the TenantBriefRefiner agent for LINDA, a conversation "
    "intelligence platform. You read aggregated call outcomes from the past "
    "weeks and extract a short, actionable playbook of what works and what "
    "doesn't.\n\n"
    "You will receive:\n"
    "* Win/loss counts by outcome_type.\n"
    "* Short snippets from won calls (high adherence, close-won, advocate).\n"
    "* Short snippets from lost/at-risk calls (churn flagged, close-lost).\n"
    "* Customer-level outcome events (upsells, churns).\n\n"
    "Respond in JSON only (no markdown fences) with this shape:\n"
    "{\n"
    '  "what_works": ["short bullets, <=15 words each, <=5 total"],\n'
    '  "what_doesnt": ["short bullets, <=15 words each, <=5 total"],\n'
    '  "top_performing_phrases": ["specific phrases agents said in wins"],\n'
    '  "common_failure_modes": ["what went wrong in losses"],\n'
    '  "winning_objection_handlers": [\n'
    '    {"objection": "...", "handler": "..."}\n'
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    "- Ground every bullet in the evidence provided. No hallucinations.\n"
    "- Prefer concrete language over vague (\"led with ROI\", not \"was good\").\n"
    "- If the sample size is tiny (<3 wins and <3 losses), return empty "
    "arrays — we'll try again next cycle.\n"
    "- Do NOT rewrite the existing playbook; you're producing the new one "
    "from scratch based on the latest window."
)


class TenantBriefRefiner:
    """Reads the last N days of interactions and refines the playbook."""

    def __init__(self, client: Optional[anthropic.AsyncAnthropic] = None) -> None:
        if client is not None:
            self._client = client
        else:
            settings = get_settings()
            self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def refine(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        window_days: int = _DEFAULT_WINDOW_DAYS,
    ) -> Dict[str, Any]:
        """Run one refine cycle for a tenant.

        Returns the new ``playbook_insights`` dict that was persisted into
        ``tenant.tenant_context``.
        """
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found")

        since = datetime.now(timezone.utc) - timedelta(days=window_days)

        interactions = list(
            (
                await db.execute(
                    select(Interaction)
                    .where(
                        Interaction.tenant_id == tenant_id,
                        Interaction.created_at >= since,
                        Interaction.outcome_type.is_not(None),
                    )
                    .order_by(Interaction.created_at.desc())
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

        aggregates = _summarise_interactions(interactions, events)

        if aggregates["wins"] + aggregates["losses"] < 3:
            new_playbook = _empty_playbook()
            new_playbook["sample_size"] = aggregates["wins"] + aggregates["losses"]
            new_playbook["last_learned_at"] = datetime.now(timezone.utc).isoformat()
            return await self._persist(db, tenant, new_playbook)

        learned = await self._call_haiku(aggregates)
        learned["sample_size"] = aggregates["wins"] + aggregates["losses"]
        learned["last_learned_at"] = datetime.now(timezone.utc).isoformat()
        return await self._persist(db, tenant, learned)

    async def _call_haiku(self, aggregates: Dict[str, Any]) -> Dict[str, Any]:
        user_blocks: List[str] = []
        user_blocks.append(
            "## Win/loss counts by outcome_type\n"
            + json.dumps(aggregates["by_outcome_type"])
        )
        user_blocks.append(
            f"## Customer events ({len(aggregates['customer_events'])} in window)\n"
            + json.dumps(aggregates["customer_events"][:20])
        )
        if aggregates["won_snippets"]:
            user_blocks.append(
                "## Sampled won/advocate calls\n"
                + "\n---\n".join(aggregates["won_snippets"])
            )
        if aggregates["lost_snippets"]:
            user_blocks.append(
                "## Sampled lost/at-risk calls\n"
                + "\n---\n".join(aggregates["lost_snippets"])
            )
        user_message = "\n\n".join(user_blocks) + "\n\nReturn the playbook JSON."

        try:
            resp = await self._client.messages.create(
                model=_MODEL,
                max_tokens=1200,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw = resp.content[0].text
            data = json.loads(raw)
        except (anthropic.APIError, json.JSONDecodeError, IndexError, KeyError):
            logger.exception("TenantBriefRefiner Haiku call failed; returning empty")
            return _empty_playbook()

        return _validate_playbook(data)

    @staticmethod
    async def _persist(
        db: AsyncSession,
        tenant: Tenant,
        playbook: Dict[str, Any],
    ) -> Dict[str, Any]:
        from backend.app.services.kb.context_builder import _validate_brief

        brief = _validate_brief(tenant.tenant_context or {})
        brief["playbook_insights"] = playbook
        tenant.tenant_context = brief
        return playbook


# ───── Helpers ───────────────────────────────────────────────


_WON_OUTCOMES = {"closed_won", "resolved", "qualified", "upsell_opportunity", "demo_scheduled", "booked_meeting"}
_LOST_OUTCOMES = {"closed_lost", "unresolved", "disqualified", "escalated"}


def _interaction_snippet(i: Interaction) -> str:
    """Compact evidence block for one call: outcome + coaching + short summary."""
    ins = i.insights or {}
    summary = (ins.get("summary") or "")[:400]
    coaching = ins.get("coaching") or {}
    score = coaching.get("script_adherence_score")
    went_well = coaching.get("what_went_well") or []
    improvements = coaching.get("improvements") or []
    objections = ins.get("competitor_mentions") or []
    blob = (
        f"[{i.outcome_type}; adherence={score}]\n"
        f"summary: {summary}\n"
        f"went_well: {', '.join(str(w) for w in went_well[:3])}\n"
        f"improvements: {', '.join(str(w) for w in improvements[:3])}"
    )
    if objections:
        obj_s = ", ".join(
            f"{o.get('name')}({'handled' if o.get('handled_well') else 'missed'})"
            for o in objections
            if isinstance(o, dict)
        )
        blob += f"\nobjections: {obj_s}"
    return blob[:_MAX_SNIPPET_CHARS]


def _summarise_interactions(
    interactions: List[Interaction],
    events: List[CustomerOutcomeEvent],
) -> Dict[str, Any]:
    by_outcome = Counter(i.outcome_type for i in interactions if i.outcome_type)

    won = [i for i in interactions if i.outcome_type in _WON_OUTCOMES]
    lost = [i for i in interactions if i.outcome_type in _LOST_OUTCOMES]

    random.shuffle(won)
    random.shuffle(lost)
    won_snippets = [_interaction_snippet(i) for i in won[:_MAX_WON_SAMPLES]]
    lost_snippets = [_interaction_snippet(i) for i in lost[:_MAX_LOST_SAMPLES]]

    return {
        "by_outcome_type": dict(by_outcome),
        "wins": len(won),
        "losses": len(lost),
        "won_snippets": won_snippets,
        "lost_snippets": lost_snippets,
        "customer_events": [
            {
                "event_type": e.event_type,
                "reason": (e.reason or "")[:200],
                "signal_strength": e.signal_strength,
            }
            for e in events
        ],
    }


def _empty_playbook() -> Dict[str, Any]:
    return {
        "what_works": [],
        "what_doesnt": [],
        "top_performing_phrases": [],
        "common_failure_modes": [],
        "winning_objection_handlers": [],
        "last_learned_at": "",
        "sample_size": 0,
    }


def _validate_playbook(data: Dict[str, Any]) -> Dict[str, Any]:
    out = _empty_playbook()
    if not isinstance(data, dict):
        return out
    for key in ("what_works", "what_doesnt", "top_performing_phrases", "common_failure_modes"):
        val = data.get(key, [])
        if isinstance(val, list):
            out[key] = [str(v)[:200] for v in val[:8]]
    handlers = data.get("winning_objection_handlers", [])
    if isinstance(handlers, list):
        cleaned: List[Dict[str, str]] = []
        for h in handlers[:8]:
            if isinstance(h, dict):
                cleaned.append(
                    {
                        "objection": str(h.get("objection", ""))[:200],
                        "handler": str(h.get("handler", ""))[:300],
                    }
                )
        out["winning_objection_handlers"] = cleaned
    return out
