"""Builder for the manager-side ``Recommended moves`` queue.

Reads the freshest ``BusinessProfile`` plus open ``ManagerAlert`` rows
for a tenant and asks Haiku to draft up to five ranked recommendations
across four categories. Output passes through the plain-English
sanitizer (em-dash strip, banned-phrase scrub; titles word-capped,
rationales deliberately not) and lands as ``ManagerRecommendation``
rows ranked by ``score``. Customer-targeted rows then get the
account-brief enrichment pass (``recommendation_enrichment``).

Runs once a day at 04:30 UTC via Celery Beat, right after
``orchestrator-daily`` has refreshed the BusinessProfile. The page
itself does zero LLM work — it reads precomputed rows.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.app.models import (
    BusinessProfile,
    Customer,
    ManagerAlert,
    ManagerRecommendation,
    Tenant,
    User,
)
from backend.app.services.plain_english import (
    MANAGER_VOICE_RULES,
    manager_voice_rules_for,
    sanitize_manager_payload,
)

from backend.app.services import model_catalog
from backend.app.services.model_router import (
    CacheableBlock,
    LLMRequest,
    TaskType,
    Tier,
    get_router,
)

logger = logging.getLogger(__name__)


HAIKU_MODEL = model_catalog.HAIKU


# Per-motion recommendation categories. Each builder pass runs once
# per domain the tenant cares about, telling Haiku which categories
# are valid in that motion. A post-hoc filter
# (``_VALID_CATEGORIES_BY_DOMAIN``) drops cross-motion contamination if
# the model still emits a sales category on a CS run.
_VALID_CATEGORIES_BY_DOMAIN: Dict[str, set] = {
    "sales": {
        "coach_rep",
        "run_campaign",
        "outreach_at_risk_customer",
        "promote_winning_script",
        # Predictive / cohort-derived (added in
        # analytics-as-recommendations).
        "prevent_lead_stall",
    },
    "customer_service": {
        "schedule_qbr",
        "flag_renewal_risk",
        "assign_expansion_play",
        "coach_csm",
        # Predictive.
        "prevent_no_touch_churn",
        "proactive_outreach_repeat_support",
    },
    "it_support": {
        "update_kb_article",
        "route_to_specialist",
        "coach_support_agent",
        "escalate_recurring_issue",
        # Cross-customer trend detector (PR ai-cross-customer-trends).
        "address_recurring_issue",
    },
    "generic": {
        "coach_rep",
        "run_campaign",
        "outreach_at_risk_customer",
        "promote_winning_script",
    },
}


_CATEGORY_LIST_BY_DOMAIN: Dict[str, str] = {
    d: ", ".join(sorted(c)) for d, c in _VALID_CATEGORIES_BY_DOMAIN.items()
}


def _system_prompt_for(domain: str) -> str:
    """Build the Haiku system prompt for one motion. Voice rules come
    from ``plain_english.manager_voice_rules_for``; the category list
    is restricted to that motion's whitelist."""
    return (
        manager_voice_rules_for(domain)
        + "\n"
        + "You produce a JSON array of up to 5 manager recommendations. Each "
        "item has keys: category (one of: "
        + _CATEGORY_LIST_BY_DOMAIN.get(domain, _CATEGORY_LIST_BY_DOMAIN["sales"])
        + "), title (≤25 words, plain English), rationale (evidence-cited; "
        "as long as the evidence deserves and no longer, never padded), "
        "target (object: rep_user_id|customer_id|"
        "script_phrase|campaign_topic|kb_article_topic depending on "
        "category, or empty), evidence (object: "
        "call_count|customer_count|dollar_estimate|sample_ids[] as "
        "available), score (0-100, expected impact). Rank by score "
        "descending. Return ONLY the JSON array, no surrounding prose."
    )


# Backward-compat alias. New code should call ``_system_prompt_for(domain)``.
_SYSTEM_PROMPT = _system_prompt_for("sales")


def build_for_tenant(session: Session, tenant: Tenant) -> List[ManagerRecommendation]:
    """Generate, sanitize, and insert recommendations for one tenant.

    Multi-motion: runs the builder once per domain that has signal
    (open alerts for that domain, or sales as the default fallback so
    the legacy single-motion behaviour is preserved). Returns the
    concatenated insert list across all motions.
    """
    profile = _latest_business_profile(session, tenant.id)
    open_alerts = _open_alerts(session, tenant.id)
    if profile is None and not open_alerts:
        return []

    # Decide which domains to run. Every domain with at least one open
    # alert gets a builder pass; plus the tenant's default_domain so a
    # sales-only tenant with no alerts but a fresh profile still gets
    # sales recommendations like before ``dom_002``.
    default_domain = (tenant.default_domain or "sales").strip() or "sales"
    domains_to_run = set()
    for a in open_alerts:
        d = a.domain or default_domain
        if d in _VALID_CATEGORIES_BY_DOMAIN:
            domains_to_run.add(d)
    if default_domain in _VALID_CATEGORIES_BY_DOMAIN:
        domains_to_run.add(default_domain)
    if not domains_to_run:
        domains_to_run.add("sales")

    inserted: List[ManagerRecommendation] = []
    for domain in domains_to_run:
        inserted.extend(_build_for_tenant_domain(session, tenant, domain, profile, open_alerts))
    return inserted


def _build_for_tenant_domain(
    session: Session,
    tenant: Tenant,
    domain: str,
    profile: Optional[BusinessProfile],
    open_alerts: List[ManagerAlert],
) -> List[ManagerRecommendation]:
    """Single-domain pass. Internal — ``build_for_tenant`` is the
    public entry point and handles multi-motion fan-out."""
    candidates = _candidate_targets(session, tenant.id)
    # Filter alerts to this motion so the Haiku prompt isn't conflating
    # sales alerts with CS evidence (or vice versa).
    motion_alerts = [
        a for a in open_alerts if (a.domain or "sales") == domain
    ][:20]

    prompt_body = {
        "tenant_id": str(tenant.id),
        "domain": domain,
        "business_profile": (profile.profile if profile else {}),
        "top_factors": (profile.top_factors if profile else []),
        "open_alerts": [
            {
                "kind": a.kind,
                "severity": a.severity,
                "title": a.title,
                "evidence": a.evidence,
            }
            for a in motion_alerts
        ],
        "playbook_insights": (tenant.tenant_context or {}).get(
            "playbook_insights", {}
        ),
        "candidates": candidates,
    }

    items = _invoke_haiku(prompt_body, domain=domain)
    if not items:
        return []

    valid_categories = _VALID_CATEGORIES_BY_DOMAIN.get(domain, set())
    inserted: List[ManagerRecommendation] = []
    expires_at = datetime.now(timezone.utc) + timedelta(days=14)
    for item in items:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if category not in valid_categories:
            continue
        title = item.get("title") or ""
        if not isinstance(title, str) or not title.strip():
            continue
        # Title stays capped (it's a UI heading); the rationale cap is
        # gone deliberately. Length is governed by the prompt ("as long
        # as the evidence deserves"), not a truncator that amputates
        # mid-analysis.
        sanitize_manager_payload(
            item,
            max_words_per_field={"title": 25},
            default_max_words=None,
        )
        row = ManagerRecommendation(
            tenant_id=tenant.id,
            domain=domain,
            category=category,
            title=item.get("title") or "",
            rationale=item.get("rationale"),
            evidence=item.get("evidence") or {},
            target=item.get("target") or {},
            score=_safe_score(item.get("score")),
            expires_at=expires_at,
        )
        session.add(row)
        inserted.append(row)
    if inserted:
        session.commit()
        # After commit, so the enrichment worker can see the rows.
        # Customer-targeted rows get the full account-brief pass;
        # tenant-level rows (coach_rep etc.) are skipped inside.
        from backend.app.services.recommendation_enrichment import (
            queue_enrichment_for,
        )

        queue_enrichment_for(inserted)
    return inserted


def build_for_all_tenants(session: Session) -> Dict[str, Any]:
    tenants = session.execute(select(Tenant)).scalars().all()
    counts: Dict[str, int] = {}
    for tenant in tenants:
        try:
            rows = build_for_tenant(session, tenant)
            counts[str(tenant.id)] = len(rows)
        except Exception:
            logger.exception(
                "build_for_tenant failed for tenant %s (non-fatal)", tenant.id
            )
            counts[str(tenant.id)] = -1
    return {"tenants_processed": len(tenants), "by_tenant": counts}


def expire_old(session: Session) -> int:
    """Sweep recommendations whose expires_at has passed. Status flips to
    ``expired`` (audit trail preserved). Called daily at 03:00 UTC."""
    now = datetime.now(timezone.utc)
    rows = (
        session.execute(
            select(ManagerRecommendation).where(
                ManagerRecommendation.status == "open",
                ManagerRecommendation.expires_at <= now,
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        r.status = "expired"
    if rows:
        session.commit()
    return len(rows)


# ── helpers ─────────────────────────────────────────────────────────────


def _latest_business_profile(session: Session, tenant_id) -> Optional[BusinessProfile]:
    return (
        session.execute(
            select(BusinessProfile)
            .where(BusinessProfile.business_tenant_id == tenant_id)
            .order_by(desc(BusinessProfile.version))
            .limit(1)
        )
        .scalar_one_or_none()
    )


def _open_alerts(session: Session, tenant_id) -> List[ManagerAlert]:
    return (
        session.execute(
            select(ManagerAlert)
            .where(
                ManagerAlert.tenant_id == tenant_id,
                ManagerAlert.acknowledged_at.is_(None),
                ManagerAlert.dismissed_at.is_(None),
                ManagerAlert.resolved_at.is_(None),
            )
            .order_by(desc(ManagerAlert.opened_at))
            .limit(20)
        )
        .scalars()
        .all()
    )


def _candidate_targets(session: Session, tenant_id) -> Dict[str, List[Dict[str, str]]]:
    """Hand the model a constrained list of valid IDs it may target.

    Without this, the model invents plausible-looking UUIDs and the
    apply endpoint 404s. Limit to active reps and recent customers.
    """
    reps = (
        session.execute(
            select(User.id, User.name).where(
                User.tenant_id == tenant_id,
                User.role.in_(("agent", "manager")),
                User.is_active.is_(True),
            )
        ).all()
    )
    customers = (
        session.execute(
            select(Customer.id, Customer.name)
            .where(Customer.tenant_id == tenant_id)
            .limit(50)
        ).all()
    )
    return {
        "reps": [{"id": str(r.id), "name": r.name or ""} for r in reps],
        "customers": [{"id": str(c.id), "name": c.name or ""} for c in customers],
    }


def _invoke_haiku(
    prompt_body: Dict[str, Any], domain: str = "sales"
) -> List[Dict[str, Any]]:
    """Single Haiku call returning the parsed JSON array. Empty list on
    any failure. ``domain`` selects the per-motion system prompt so the
    voice rules and category whitelist match the run."""
    try:
        resp = get_router().invoke(
            LLMRequest(
                task_type=TaskType.GENERIC,
                forced_tier=Tier.HAIKU,
                user_message=json.dumps(prompt_body, default=str),
                system_blocks=[CacheableBlock(text=_system_prompt_for(domain), cache=True)],
                max_tokens=2048,
                temperature=0.0,
                call_site=f"manager_recommendations_{domain}",
            )
        )
        text = resp.text.strip()
        if not text:
            return []
        # Tolerate code-fence wrappers — the prompt forbids them but
        # the safety net costs almost nothing.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("recommendations"), list):
            return parsed["recommendations"]
        return []
    except Exception:
        logger.exception("Haiku recommendation call failed (returning empty list)")
        return []


def _safe_score(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v < 0:
        return 0.0
    if v > 100:
        return 100.0
    return v
