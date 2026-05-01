"""Demo / sandbox sample data seeder.

A fresh trial tenant is otherwise an empty dashboard (no interactions,
no scorecards, no action items) — bad first impression. This module
populates a tenant with a curated, realistic snapshot so the SPA shows
data immediately on first login.

The seeder is **idempotent**: a tenant that already has interactions is
treated as "seeded" and we no-op rather than duplicate. Re-running on a
mostly-empty tenant tops up missing artifacts (scorecards, KB docs,
webhooks) without re-creating interactions.

Usage:
    from backend.app.services.demo_seeder import seed_demo_data

    counts = await seed_demo_data(db, tenant=tenant, admin_user=admin_user)

The signup flow (``/trial/signup``) calls this on tenant creation when
``body.seed_demo_data`` is true (default). An admin endpoint
(``POST /admin/seed-demo-data``) wraps the same call for the case where
an existing tenant wants the demo content backfilled.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    ActionItem,
    Interaction,
    InteractionFeatures,
    KBDocument,
    ScorecardTemplate,
    Tenant,
    User,
    Webhook,
)

logger = logging.getLogger(__name__)


# ── Fixture data ─────────────────────────────────────────────────────────


_SCORECARD_FIXTURES = [
    {
        "name": "Discovery call rubric",
        "criteria": [
            {"key": "rapport", "label": "Built rapport in opening", "weight": 1.0},
            {"key": "needs", "label": "Uncovered customer needs", "weight": 2.0},
            {"key": "qualification", "label": "Confirmed budget & timeline", "weight": 2.0},
            {"key": "next_steps", "label": "Set explicit next steps", "weight": 1.0},
        ],
        "channel_filter": ["voice"],
        "is_default": True,
    },
    {
        "name": "Support handoff",
        "criteria": [
            {"key": "verification", "label": "Verified identity", "weight": 1.0},
            {"key": "issue_clarity", "label": "Reframed the issue back to caller", "weight": 1.5},
            {"key": "resolution_path", "label": "Owned a clear resolution path", "weight": 2.0},
            {"key": "followup", "label": "Scheduled follow-up", "weight": 1.0},
        ],
        "channel_filter": ["voice", "email", "chat"],
        "is_default": False,
    },
]


# 8 sample interactions across all three channels (4 voice, 2 email, 2 chat).
# Each carries a short transcript snippet + insights with sentiment,
# churn_risk_signal, upsell_signal, topics, summary, action_items.
_INTERACTION_FIXTURES = [
    {
        "channel": "voice",
        "title": "Discovery: Acme Co",
        "duration_seconds": 32,
        "transcript": [
            {"speaker": "agent", "text": "Hey Marcus, thanks for jumping on. Walk me through what prompted the demo request."},
            {"speaker": "customer", "text": "Honestly, our agents are drowning in QA spreadsheets. We listen to maybe 1% of calls."},
            {"speaker": "agent", "text": "Yeah, that's the rule. What would change for the team if every call got scored?"},
            {"speaker": "customer", "text": "Coaching would actually be data-driven, not vibes-driven."},
        ],
        "insights": {
            "sentiment": 7.4,
            "churn_risk_signal": 0.18,
            "upsell_signal": 0.62,
            "topics": ["QA workflow", "coaching", "automation"],
            "summary": "Strong fit — manual QA pain, eager for automation.",
            "action_items": ["Send pricing PDF", "Schedule technical deep-dive"],
        },
        "outcome_type": "qualified",
        "outcome_value": 1.0,
    },
    {
        "channel": "voice",
        "title": "Renewal check-in: Globex",
        "duration_seconds": 28,
        "transcript": [
            {"speaker": "agent", "text": "Want to make sure we're tracking before renewal — anything blocking your team?"},
            {"speaker": "customer", "text": "The dashboard's slower than it was last quarter. Two of my managers complained."},
            {"speaker": "agent", "text": "Got it, I'll get engineering on that today and circle back."},
        ],
        "insights": {
            "sentiment": 4.2,
            "churn_risk_signal": 0.71,
            "upsell_signal": 0.10,
            "topics": ["performance", "renewal", "support"],
            "summary": "At-risk renewal — performance regression complaints.",
            "action_items": ["Open perf ticket", "Email status update by EOD"],
        },
        "outcome_type": "at_risk",
        "outcome_value": -0.5,
    },
    {
        "channel": "voice",
        "title": "Cold call: Initech",
        "duration_seconds": 19,
        "transcript": [
            {"speaker": "agent", "text": "Hey, this is Riley from LINDA — quick question about your call review process."},
            {"speaker": "customer", "text": "We don't really do reviews. Send me an email if you want."},
        ],
        "insights": {
            "sentiment": 3.5,
            "churn_risk_signal": 0.0,
            "upsell_signal": 0.05,
            "topics": ["cold outreach"],
            "summary": "Cold call deflected — email fallback.",
            "action_items": ["Send intro email"],
        },
        "outcome_type": "deflected",
        "outcome_value": 0.0,
    },
    {
        "channel": "voice",
        "title": "Support: Pied Piper",
        "duration_seconds": 41,
        "transcript": [
            {"speaker": "customer", "text": "I can't get the webhook to fire. It's been queued for an hour."},
            {"speaker": "agent", "text": "I see it — your endpoint is returning 502. Did you ship a deploy this morning?"},
            {"speaker": "customer", "text": "Yeah, we did. Let me roll that back and re-trigger."},
        ],
        "insights": {
            "sentiment": 6.1,
            "churn_risk_signal": 0.22,
            "upsell_signal": 0.0,
            "topics": ["webhooks", "delivery", "support"],
            "summary": "Webhook 502 traced to customer-side deploy.",
            "action_items": ["Verify replay after rollback"],
        },
        "outcome_type": "resolved",
        "outcome_value": 0.7,
    },
    {
        "channel": "email",
        "title": "Re: Pricing question",
        "subject": "Re: Pricing question",
        "from_address": "billing@acme.example",
        "to_addresses": ["sales@your-tenant.example"],
        "raw_text": (
            "Thanks for the call yesterday. Quick clarification — does the Growth tier "
            "include the live coaching seats or are those an add-on? We'd want at "
            "least 5 to start."
        ),
        "insights": {
            "sentiment": 6.8,
            "churn_risk_signal": 0.05,
            "upsell_signal": 0.78,
            "topics": ["pricing", "live coaching", "expansion"],
            "summary": "Inbound expansion question — needs 5 live coaching seats.",
            "action_items": ["Quote 5 seats", "Confirm with CSM"],
        },
        "outcome_type": "expansion",
        "outcome_value": 0.9,
    },
    {
        "channel": "email",
        "title": "Cancellation request",
        "subject": "Cancellation request",
        "from_address": "ops@unhappy.example",
        "to_addresses": ["support@your-tenant.example"],
        "raw_text": (
            "We've decided to wind down our pilot. The signal-to-noise on action items "
            "wasn't where we needed it for the volume we're handling. Please cancel "
            "effective end of the month."
        ),
        "insights": {
            "sentiment": 2.5,
            "churn_risk_signal": 0.95,
            "upsell_signal": 0.0,
            "topics": ["cancellation", "action items", "noise"],
            "summary": "Cancellation citing action-item signal-to-noise.",
            "action_items": ["Save attempt: offer tuning session"],
        },
        "outcome_type": "churned",
        "outcome_value": -1.0,
    },
    {
        "channel": "chat",
        "title": "Live chat: trial walkthrough",
        "raw_text": (
            "[customer] How do I import historical calls?\n"
            "[agent] You can drag a folder of mp3s into /interactions or POST to "
            "/api/v1/interactions/upload. Bulk endpoint accepts up to 500 files at once.\n"
            "[customer] Perfect, going to try it now."
        ),
        "insights": {
            "sentiment": 7.2,
            "churn_risk_signal": 0.0,
            "upsell_signal": 0.34,
            "topics": ["onboarding", "bulk upload"],
            "summary": "Onboarding question — bulk upload path explained.",
            "action_items": [],
        },
        "outcome_type": "resolved",
        "outcome_value": 0.5,
    },
    {
        "channel": "chat",
        "title": "Live chat: feature request",
        "raw_text": (
            "[customer] Can you push action items into Notion?\n"
            "[agent] Notion isn't a first-class CRM target yet — Salesforce, HubSpot, "
            "and Zendesk are wired. Webhooks can fan out anywhere though.\n"
            "[customer] OK, I'll wire it through Zapier."
        ),
        "insights": {
            "sentiment": 5.5,
            "churn_risk_signal": 0.10,
            "upsell_signal": 0.20,
            "topics": ["integrations", "Notion", "feature request"],
            "summary": "Feature request: native Notion target. Workaround offered.",
            "action_items": ["Add Notion to integrations roadmap doc"],
        },
        "outcome_type": "feedback",
        "outcome_value": 0.2,
    },
]


# 6 action items derived from the seeded interactions. Distribution:
# 3 pending, 2 done, 1 snoozed; mix of priorities.
_ACTION_ITEM_FIXTURES = [
    {
        "interaction_idx": 0,
        "title": "Send Acme pricing PDF",
        "description": "Discovery call — Marcus asked for a 5-seat quote on Growth tier.",
        "priority": "high",
        "status": "pending",
        "category": "sales",
    },
    {
        "interaction_idx": 0,
        "title": "Schedule technical deep-dive",
        "description": "Acme: 30-minute screenshare with their data lead.",
        "priority": "medium",
        "status": "pending",
        "category": "sales",
    },
    {
        "interaction_idx": 1,
        "title": "File Globex perf ticket",
        "description": "Dashboard latency regression flagged at renewal check-in.",
        "priority": "high",
        "status": "done",
        "category": "support",
    },
    {
        "interaction_idx": 3,
        "title": "Verify Pied Piper webhook replay",
        "description": "After customer deploy rollback — confirm 200s on /webhooks.",
        "priority": "medium",
        "status": "done",
        "category": "support",
    },
    {
        "interaction_idx": 4,
        "title": "Quote 5 live coaching seats for Acme",
        "description": "Growth tier expansion — bundle pricing requested.",
        "priority": "high",
        "status": "pending",
        "category": "sales",
    },
    {
        "interaction_idx": 7,
        "title": "Add Notion to integrations roadmap",
        "description": "Recurring request — feature surfaced in chat.",
        "priority": "low",
        "status": "snoozed",
        "category": "product",
    },
]


_KB_DOC_FIXTURES = [
    {
        "title": "Sales playbook: discovery → demo",
        "tags": ["sales", "playbook"],
        "source_type": "seeded",
        "content": (
            "# Sales playbook: discovery → demo\n\n"
            "## Goal\nLeave every discovery call with a quantified pain, a "
            "qualified buyer, and an explicit next step. If you can't write "
            "those three sentences after the call, the call wasn't a "
            "discovery call.\n\n"
            "## Pre-call\n- Pull the contact's company news from the last 30 days.\n"
            "- Skim their pricing page so you can ask 'is X why you're shopping?'\n"
            "- Set the mutual-action-plan template to 'Discovery'.\n\n"
            "## Opening\nLead with curiosity, not a pitch. 'What prompted you to "
            "shop now?' is the highest-yield question we have. Don't anchor on "
            "features — let them describe the pain in their words.\n\n"
            "## Mid-call\nUse the 'so what' chain three times. Every pain has a "
            "downstream cost; the cost is what funds the deal. If they say "
            "'agents miss action items,' your follow-up is 'so what does that "
            "cost the team in a typical week?' Don't accept abstractions.\n\n"
            "## Closing\nNever leave without a written next step. The single "
            "biggest predictor of close rate at our stage is whether the next "
            "step is on the customer's calendar before you hang up. 'I'll send "
            "an email' is not a next step.\n\n"
            "## Disqualification\nIt is OK — actively good — to disqualify a "
            "deal in discovery. A short pipeline of real deals always beats a "
            "long pipeline of hopeful ones. Use the 'medic' framework: "
            "metrics, economic buyer, decision criteria, decision process, "
            "identify pain, champion. Two missing → at-risk. Three → walk.\n\n"
            "## After the call\nLog the qualified pain, the next step, and the "
            "owner before you do anything else. The CRM is the system of "
            "record; your notebook is not. If the call had a churn signal, "
            "tag it so the renewal team sees it.\n"
        ),
    },
    {
        "title": "Objection handling: top 10",
        "tags": ["sales", "objections"],
        "source_type": "seeded",
        "content": (
            "# Objection handling: top 10\n\n"
            "Every objection is data — it tells you what the customer is "
            "actually deciding on. Don't argue the surface; reframe to the "
            "underlying concern.\n\n"
            "## 1. 'Too expensive.'\nReframe to value-per-seat-per-month. If "
            "they save 10 minutes of QA time per agent per day, the math is "
            "obvious. Don't discount until you've quantified.\n\n"
            "## 2. 'We're already using a competitor.'\nAsk what they wish it "
            "did better. The answer almost always maps to something we do.\n\n"
            "## 3. 'We need to think about it.'\nThis is a missing decision "
            "criterion. Ask 'what would you need to know to decide?' and "
            "write the answer down. That's your next email.\n\n"
            "## 4. 'I need to ask my boss.'\nGreat — let's get them on the "
            "next call. Coaching the champion to sell internally is the lowest-"
            "leverage thing you can do; getting the EB live is the highest.\n\n"
            "## 5. 'We don't have budget.'\nBudget is a question of priority. "
            "If there is no budget *anywhere* on their roadmap, you have a "
            "qualification problem, not a pricing problem.\n\n"
            "## 6. 'It's not a priority right now.'\nFair. Get the trigger that "
            "would make it a priority and check in then. Don't pitch harder.\n\n"
            "## 7. 'Send me more info.'\nThis is the conversational equivalent "
            "of unsubscribing. Try one redirect: 'happy to — what's the "
            "specific question you want me to answer?' If they can't name it, "
            "let it go.\n\n"
            "## 8. 'Your security policy.'\nSend the SOC 2 + DPA before they "
            "ask twice. Procurement can kill a deal in days; pre-empt.\n\n"
            "## 9. 'We tried this category before, didn't work.'\nUnpack what "
            "didn't work. Usually it's a deployment / change-management "
            "failure, not a product failure. Sell the rollout, not the tool.\n\n"
            "## 10. 'We'll build it ourselves.'\nMaintenance is the hidden "
            "cost. Ask who's going to own it on year three.\n"
        ),
    },
]


# ── Idempotency check ──────────────────────────────────────────────────


async def _tenant_already_seeded(db: AsyncSession, tenant_id) -> bool:
    """A tenant counts as 'already seeded' if it has any interactions.

    We anchor on interactions because the dashboard is empty without
    them — the seeder's primary job is to populate that view. Top-up
    of missing artifacts (scorecards, KB docs, webhooks) is allowed
    even when interactions exist; see the per-resource checks below.
    """
    stmt = select(func.count(Interaction.id)).where(
        Interaction.tenant_id == tenant_id
    )
    count = (await db.execute(stmt)).scalar_one()
    return bool(count)


# ── Public seeder ──────────────────────────────────────────────────────


async def seed_demo_data(
    db: AsyncSession,
    *,
    tenant: Tenant,
    admin_user: Optional[User] = None,
) -> Dict[str, int]:
    """Populate ``tenant`` with sample scorecards, interactions, action
    items, KB docs, and a sample webhook.

    Idempotent: if the tenant already has interactions, the
    interaction / action-item / scoring section is skipped. Scorecards,
    KB docs, and the sample webhook top up by name / URL match.

    Returns a dict of created counts, e.g.
    ``{"scorecards": 2, "interactions": 8, "action_items": 6, "kb_docs": 2, "webhooks": 1}``.
    Already-present rows count zero — callers that need to detect a
    no-op can sum the dict.
    """
    counts = {
        "scorecards": 0,
        "interactions": 0,
        "action_items": 0,
        "kb_docs": 0,
        "webhooks": 0,
    }

    # Look up admin user if not provided — fall back to the first admin.
    if admin_user is None:
        admin_user = (
            await db.execute(
                select(User)
                .where(User.tenant_id == tenant.id, User.role == "admin")
                .order_by(User.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()

    # ── Scorecards (top up by name) ────────────────────────────────────
    existing_scorecards = {
        name
        for (name,) in (
            await db.execute(
                select(ScorecardTemplate.name).where(
                    ScorecardTemplate.tenant_id == tenant.id
                )
            )
        ).all()
    }
    for spec in _SCORECARD_FIXTURES:
        if spec["name"] in existing_scorecards:
            continue
        db.add(
            ScorecardTemplate(
                tenant_id=tenant.id,
                name=spec["name"],
                criteria=spec["criteria"],
                channel_filter=spec["channel_filter"],
                is_default=spec["is_default"],
            )
        )
        counts["scorecards"] += 1

    # ── Interactions + action items (skip wholesale if already seeded) ─
    if await _tenant_already_seeded(db, tenant.id):
        logger.info(
            "demo_seeder: tenant %s already has interactions — skipping the "
            "interactions / action_items section",
            tenant.id,
        )
    else:
        # Stagger created_at across the last week so the dashboard's
        # "recent interactions" view shows realistic time spread.
        now = datetime.now(timezone.utc)
        created_interactions: List[Interaction] = []
        for idx, spec in enumerate(_INTERACTION_FIXTURES):
            interaction = Interaction(
                tenant_id=tenant.id,
                agent_id=admin_user.id if admin_user is not None else None,
                channel=spec["channel"],
                source="demo_seed",
                title=spec.get("title"),
                transcript=spec.get("transcript", []),
                raw_text=spec.get("raw_text"),
                subject=spec.get("subject"),
                from_address=spec.get("from_address"),
                to_addresses=spec.get("to_addresses", []),
                duration_seconds=spec.get("duration_seconds"),
                status="completed",
                engine="demo",
                insights=spec.get("insights", {}),
                outcome_type=spec.get("outcome_type"),
                outcome_value=spec.get("outcome_value"),
                outcome_source="seeded",
                outcome_captured_at=now - timedelta(hours=idx * 6),
            )
            db.add(interaction)
            created_interactions.append(interaction)
            counts["interactions"] += 1

        # Flush so action_items can FK against the new ids.
        await db.flush()

        # InteractionFeatures is consumed by the scorer / orchestrator —
        # seed an empty row per interaction so downstream code doesn't
        # 404 looking for it on the demo dashboard.
        for interaction in created_interactions:
            db.add(
                InteractionFeatures(
                    interaction_id=interaction.id,
                    tenant_id=tenant.id,
                    deterministic={},
                    llm_structured=interaction.insights or {},
                    proxy_outcomes={},
                    scorer_versions={},
                )
            )

        # Action items reference interactions by index.
        for spec in _ACTION_ITEM_FIXTURES:
            interaction = created_interactions[spec["interaction_idx"]]
            db.add(
                ActionItem(
                    interaction_id=interaction.id,
                    tenant_id=tenant.id,
                    assigned_to=admin_user.id if admin_user is not None else None,
                    title=spec["title"],
                    description=spec.get("description"),
                    category=spec.get("category"),
                    priority=spec.get("priority", "medium"),
                    status=spec.get("status", "pending"),
                    automation_status="pending",
                )
            )
            counts["action_items"] += 1

    # ── KB docs (top up by title) ──────────────────────────────────────
    existing_kb_titles = {
        t
        for (t,) in (
            await db.execute(
                select(KBDocument.title).where(KBDocument.tenant_id == tenant.id)
            )
        ).all()
        if t is not None
    }
    for spec in _KB_DOC_FIXTURES:
        if spec["title"] in existing_kb_titles:
            continue
        db.add(
            KBDocument(
                tenant_id=tenant.id,
                title=spec["title"],
                content=spec["content"],
                source_type=spec["source_type"],
                tags=spec["tags"],
            )
        )
        counts["kb_docs"] += 1

    # ── Sample webhook (top up by URL) ─────────────────────────────────
    sample_webhook_url = "https://webhook.site/sample-linda"
    existing_webhook = (
        await db.execute(
            select(Webhook.id).where(
                Webhook.tenant_id == tenant.id, Webhook.url == sample_webhook_url
            )
        )
    ).scalar_one_or_none()
    if existing_webhook is None:
        db.add(
            Webhook(
                tenant_id=tenant.id,
                url=sample_webhook_url,
                events=["*"],
                # Active so the dispatcher actually attempts delivery —
                # the URL will fail, populating the retry UI for the
                # customer to explore. Secret is randomized so the
                # dashboard signature column has something to render.
                secret=secrets.token_urlsafe(24),
                active=True,
                consecutive_failures=0,
            )
        )
        counts["webhooks"] += 1

    await db.commit()
    logger.info(
        "demo_seeder: tenant=%s created=%s",
        tenant.id,
        counts,
    )
    return counts
