"""Per-recommendation enrichment: compose a situation-specific brief.

The deterministic detectors (``cohort_recommendations``) and the daily
builder (``manager_recommendation_builder``) are good triggers and bad
writers: they know *that* an account needs attention, not what the
account manager should actually walk in with. This pass closes that gap.

For each customer-targeted ``ManagerRecommendation`` it:

1. Assembles the account's full context deterministically (recent
   interactions with their analysis, tracked concerns, open commitments
   on both sides, support-case history, renewal state, KB matches).
2. Hands trigger + context to Sonnet with a *palette* of typed sections
   (``SECTION_KINDS``) and lets the model compose whichever combination
   the situation supports. There is no fixed template: an imminent
   renewal with rich history may earn a play, a draft email, landmines,
   and cited evidence; a thin-data stall may earn a headline and one
   paragraph. The form is decided per recommendation.
3. Persists the result on ``ManagerRecommendation.brief``; the
   deterministic ``rationale`` stays untouched as the fallback when
   enrichment is disabled or fails.

No word caps anywhere in this pass: length is governed by the voice
rules ("exactly as long as the evidence deserves"), not a truncator.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    Commitment,
    Customer,
    CustomerCommitment,
    CustomerConcern,
    Interaction,
    ManagerRecommendation,
    SupportCase,
)
from backend.app.services.model_router import (
    CacheableBlock,
    LLMRequest,
    TaskType,
    Tier,
    get_router,
)
from backend.app.services.plain_english import (
    longform_voice_rules_for,
    sanitize_manager_payload,
)

logger = logging.getLogger(__name__)


# ── The section palette ─────────────────────────────────────────────────
#
# Each kind is a *move* the brief can make. The composer picks any
# combination (including just one) based on what the context supports;
# the SPA renders sections generically (title + body + items), so adding
# a kind here needs no frontend work.

SECTION_KINDS = (
    "situation",
    "why_now",
    "play",
    "talking_points",
    "draft_message",
    "watch_out",
    "evidence",
    "playbook",
    "commitments",
    "success",
)

_PALETTE_GUIDANCE = {
    "situation": (
        "Where the account stands, as a short narrative. Use when the "
        "trigger alone doesn't tell the story."
    ),
    "why_now": (
        "The stakes and the clock. Use when timing matters: a renewal "
        "window, a cooling deal, frustration building across support "
        "cases."
    ),
    "play": (
        "The recommended course of action: the most direct route to the "
        "outcome, concrete steps in order. Almost always earns its place."
    ),
    "talking_points": (
        "items: specific points for the conversation, each anchored to "
        "something this customer actually said or did."
    ),
    "draft_message": (
        "body: a ready-to-send email or call opener, personalized from "
        "the context. Only when outreach is the play AND the context "
        "gives enough to personalize; a generic draft is worse than none."
    ),
    "watch_out": (
        "Landmines: topics, framings, or approaches to avoid, each "
        "anchored to past friction on this account."
    ),
    "evidence": (
        "items: dated facts from the history that justify the play, so "
        "the reader can trust it without re-reading the account."
    ),
    "playbook": (
        "items: knowledge-base guidance that applies, each with why it "
        "fits this account. Only when KB matches were provided."
    ),
    "commitments": (
        "items: open promises in either direction that this outreach "
        "should honor or chase."
    ),
    "success": (
        "What a good outcome of this move looks like, so the reader "
        "knows when it worked."
    ),
}

# How many rows of each source feed the prompt. These bound prompt size,
# not analysis depth: they're generous relative to what one account
# accumulates between recommendations.
_MAX_INTERACTIONS = 12
_MAX_CONCERNS = 10
_MAX_COMMITMENTS = 10
_MAX_SUPPORT_CASES = 10
_SUPPORT_CASE_WINDOW_DAYS = 180
_KB_TOP_K = 3


def _system_prompt(domain: Optional[str]) -> str:
    palette = "\n".join(
        f"- {kind}: {_PALETTE_GUIDANCE[kind]}" for kind in SECTION_KINDS
    )
    return (
        longform_voice_rules_for(domain)
        + "\n"
        + "You are preparing an account manager to act on one "
        "recommendation about one account. You receive the trigger (why "
        "the account was flagged) and the account's full context. Compose "
        "a brief from these section kinds:\n"
        + palette
        + "\n\nCOMPOSITION RULES\n"
        "- Include ONLY the sections this situation genuinely supports. "
        "Any combination, any count: thin context correctly yields a "
        "headline plus one or two sections. Never pad a section into "
        "existence.\n"
        "- Order sections by usefulness to the reader, most useful first.\n"
        "- headline: the single move, one imperative sentence, specific "
        "to this account. Not a summary of the brief.\n"
        "- Every date, name, number, and quote must come from the "
        "provided context. If the context doesn't support a section, "
        "leave it out.\n"
        "- Spell out 'account manager'; never abbreviate to AM.\n\n"
        "OUTPUT: only a JSON object, no surrounding prose:\n"
        '{"headline": str, "sections": [{"kind": <one of the kinds '
        'above>, "title": <short label>, "body": <prose, optional>, '
        '"items": [<str>, ...] (optional)}]}\n'
        "Each section must have body, items, or both."
    )


# ── Context assembly (deterministic; no LLM) ─────────────────────────────


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _interaction_digest(row: Interaction) -> Dict[str, Any]:
    insights = row.insights if isinstance(row.insights, dict) else {}
    digest: Dict[str, Any] = {
        "date": _iso(row.created_at),
        "motion": row.domain,
        "channel": row.channel,
        "summary": insights.get("summary") or insights.get("headline"),
        "sentiment": insights.get("sentiment_overall"),
    }
    for key in ("churn_risk_signal", "upsell_signal"):
        if insights.get(key):
            digest[key] = insights[key]
    moments = insights.get("key_moments")
    if isinstance(moments, list) and moments:
        digest["key_moments"] = moments[:3]
    competitors = insights.get("competitor_mentions")
    if isinstance(competitors, list) and competitors:
        digest["competitor_mentions"] = competitors
    return {k: v for k, v in digest.items() if v is not None}


async def assemble_account_context(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    customer: Customer,
    *,
    kb_query: Optional[str] = None,
) -> Dict[str, Any]:
    """Pull every per-account signal we track into one JSON-able dict.

    Pure queries plus one optional KB vector search; the LLM sees the
    output verbatim as the ``account`` half of the enrichment prompt.
    """
    now = datetime.now(timezone.utc)

    interactions = (
        (
            await session.execute(
                select(Interaction)
                .where(
                    Interaction.tenant_id == tenant_id,
                    Interaction.customer_id == customer.id,
                )
                .order_by(desc(Interaction.created_at))
                .limit(_MAX_INTERACTIONS)
            )
        )
        .scalars()
        .all()
    )

    concerns = (
        (
            await session.execute(
                select(CustomerConcern)
                .where(
                    CustomerConcern.tenant_id == tenant_id,
                    CustomerConcern.customer_id == customer.id,
                    CustomerConcern.status.in_(("active", "monitoring")),
                )
                .order_by(desc(CustomerConcern.last_seen_at))
                .limit(_MAX_CONCERNS)
            )
        )
        .scalars()
        .all()
    )

    customer_commitments = (
        (
            await session.execute(
                select(CustomerCommitment)
                .where(
                    CustomerCommitment.tenant_id == tenant_id,
                    CustomerCommitment.customer_id == customer.id,
                    CustomerCommitment.status.in_(("open", "broken")),
                )
                .limit(_MAX_COMMITMENTS)
            )
        )
        .scalars()
        .all()
    )

    our_commitments = (
        (
            await session.execute(
                select(Commitment)
                .where(
                    Commitment.tenant_id == tenant_id,
                    Commitment.customer_id == customer.id,
                    Commitment.status.in_(("pending", "overdue")),
                )
                .limit(_MAX_COMMITMENTS)
            )
        )
        .scalars()
        .all()
    )

    support_cases = (
        (
            await session.execute(
                select(SupportCase)
                .where(
                    SupportCase.tenant_id == tenant_id,
                    SupportCase.customer_id == customer.id,
                    SupportCase.opened_at
                    >= now - timedelta(days=_SUPPORT_CASE_WINDOW_DAYS),
                )
                .order_by(desc(SupportCase.opened_at))
                .limit(_MAX_SUPPORT_CASES)
            )
        )
        .scalars()
        .all()
    )

    renewal: Dict[str, Any] = {
        "renewal_date": _iso(customer.renewal_date),
        "health_score": customer.health_score,
        "onboarding_status": customer.onboarding_status,
    }
    if customer.renewal_date is not None:
        renewal["days_to_renewal"] = (
            customer.renewal_date - now.date()
        ).days

    context: Dict[str, Any] = {
        "customer_name": customer.name,
        "renewal": {k: v for k, v in renewal.items() if v is not None},
        "recent_interactions": [
            _interaction_digest(r) for r in interactions
        ],
        "tracked_concerns": [
            {
                "topic": c.topic,
                "description": c.description,
                "severity": c.severity,
                "status": c.status,
                "last_seen": _iso(c.last_seen_at),
            }
            for c in concerns
        ],
        "customer_promises": [
            {
                "description": c.description,
                "quote": c.quote,
                "due_date": _iso(c.due_date),
                "status": c.status,
            }
            for c in customer_commitments
        ],
        "our_open_commitments": [
            {
                "text": c.text,
                "actor_side": c.actor_side,
                "due_date": _iso(c.due_date),
                "status": c.status,
                "evidence_quote": c.evidence_excerpt,
            }
            for c in our_commitments
        ],
        "support_cases": [
            {
                "subject": c.subject,
                "status": c.status,
                "priority": c.priority,
                "opened": _iso(c.opened_at),
                "resolved": _iso(c.resolved_at),
            }
            for c in support_cases
        ],
    }

    if kb_query:
        context["kb_matches"] = await _kb_matches(
            session, tenant_id, customer.id, kb_query
        )

    return context


async def _kb_matches(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    query: str,
) -> List[Dict[str, Any]]:
    """Top-K KB hits for the trigger topic, customer-scoped.

    Best-effort: retrieval failures (Voyage down, empty index) yield an
    empty list and the composer simply won't emit a ``playbook`` section.
    """
    try:
        from backend.app.services.kb.retrieval import RetrievalService

        hits = await RetrievalService().search(
            session,
            tenant_id,
            query,
            k=_KB_TOP_K,
            customer_id=customer_id,
        )
    except Exception:
        logger.exception("KB retrieval failed during enrichment (non-fatal)")
        return []
    return [
        {
            "title": h.doc_title,
            "excerpt": (h.text or "")[:500],
        }
        for h in hits
    ]


# ── Composition (one Sonnet call) ────────────────────────────────────────


def _validate_brief(raw: Any) -> Optional[Dict[str, Any]]:
    """Coerce model output into the stored brief shape, or None.

    Unknown section kinds and empty sections are dropped rather than
    failing the whole brief; a brief with a headline and zero surviving
    sections is still rejected (nothing to show beyond the rationale).
    """
    if not isinstance(raw, dict):
        return None
    headline = raw.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        return None
    sections: List[Dict[str, Any]] = []
    for sec in raw.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        kind = sec.get("kind")
        if kind not in SECTION_KINDS:
            continue
        body = sec.get("body")
        items = [
            i for i in (sec.get("items") or []) if isinstance(i, str) and i.strip()
        ]
        if not (isinstance(body, str) and body.strip()) and not items:
            continue
        clean: Dict[str, Any] = {
            "kind": kind,
            "title": sec.get("title") if isinstance(sec.get("title"), str) else "",
        }
        if isinstance(body, str) and body.strip():
            clean["body"] = body.strip()
        if items:
            clean["items"] = items
        sections.append(clean)
    if not sections:
        return None
    return {"headline": headline.strip(), "sections": sections}


async def compose_brief(
    rec: ManagerRecommendation, account_context: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """One Sonnet call: trigger + context in, validated brief out.

    Returns None on any failure so the caller leaves the recommendation
    on its deterministic rationale.
    """
    payload = {
        "trigger": {
            "category": rec.category,
            "motion": rec.domain,
            "title": rec.title,
            "detector_rationale": rec.rationale,
            "detector_evidence": rec.evidence or {},
        },
        "account": account_context,
    }
    try:
        resp = await get_router().ainvoke(
            LLMRequest(
                task_type=TaskType.GENERIC,
                forced_tier=Tier.SONNET,
                user_message=json.dumps(payload, default=str),
                system_blocks=[
                    CacheableBlock(text=_system_prompt(rec.domain), cache=True)
                ],
                max_tokens=3000,
                temperature=0.0,
                call_site="recommendation_enrichment",
            )
        )
        brief = _validate_brief(resp.parse_json())
    except Exception:
        logger.exception(
            "Enrichment composition failed for recommendation %s", rec.id
        )
        return None
    if brief is None:
        logger.warning(
            "Enrichment output failed validation for recommendation %s", rec.id
        )
        return None
    # Dash + banned-phrase scrub only. Deliberately no word caps: the
    # whole point of the brief is that length follows evidence.
    sanitize_manager_payload(brief, default_max_words=None)
    return brief


# ── Entry points ─────────────────────────────────────────────────────────


def _target_customer_id(rec: ManagerRecommendation) -> Optional[uuid.UUID]:
    raw = (rec.target or {}).get("customer_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


async def enrich_by_id(rec_id: str) -> Dict[str, Any]:
    """Load one recommendation, enrich it, commit. Celery entry point.

    Every early-out returns a status string so the task result explains
    itself in Flower / logs without a stack trace.
    """
    from backend.app.db import async_session

    async with async_session() as session:
        rec = await session.get(ManagerRecommendation, uuid.UUID(rec_id))
        if rec is None:
            return {"status": "missing", "rec_id": rec_id}
        if rec.status != "open":
            return {"status": "skipped_not_open", "rec_id": rec_id}
        if rec.brief:
            return {"status": "already_enriched", "rec_id": rec_id}
        customer_id = _target_customer_id(rec)
        if customer_id is None:
            return {"status": "no_customer_target", "rec_id": rec_id}
        customer = await session.get(Customer, customer_id)
        if customer is None or customer.tenant_id != rec.tenant_id:
            return {"status": "customer_not_found", "rec_id": rec_id}

        kb_query = rec.title
        context = await assemble_account_context(
            session, rec.tenant_id, customer, kb_query=kb_query
        )
        brief = await compose_brief(rec, context)
        if brief is None:
            return {"status": "compose_failed", "rec_id": rec_id}

        rec.brief = brief
        rec.enriched_at = datetime.now(timezone.utc)
        await session.commit()
        return {
            "status": "enriched",
            "rec_id": rec_id,
            "sections": [s["kind"] for s in brief["sections"]],
        }


def queue_enrichment_for(rows: List[ManagerRecommendation]) -> int:
    """Enqueue the enrichment task for freshly inserted recommendations.

    Call AFTER commit so the worker can see the rows. Flag-gated,
    customer-targeted rows only, never raises: enrichment is an upgrade
    on top of a recommendation that already works without it.
    """
    from backend.app.config import get_settings

    if not get_settings().RECOMMENDATION_ENRICHMENT_ENABLED:
        return 0
    queued = 0
    for row in rows:
        if _target_customer_id(row) is None:
            continue
        try:
            from backend.app.tasks import enrich_manager_recommendation

            enrich_manager_recommendation.delay(str(row.id))
            queued += 1
        except Exception:
            logger.exception(
                "Failed to enqueue enrichment for recommendation %s "
                "(non-fatal)",
                row.id,
            )
    return queued
