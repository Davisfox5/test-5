"""Builder for the manager-side ``Recommended moves`` queue.

Reads the freshest ``BusinessProfile`` plus open ``ManagerAlert`` rows
for a tenant and asks Haiku to draft up to five ranked recommendations
across four categories. Output passes through the plain-English
sanitizer (em-dash strip, banned-phrase scrub, word-cap) and lands as
``ManagerRecommendation`` rows ranked by ``score``.

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
from backend.app.services.llm_client import get_anthropic
from backend.app.services.plain_english import (
    MANAGER_VOICE_RULES,
    sanitize_manager_payload,
)

logger = logging.getLogger(__name__)


HAIKU_MODEL = "claude-haiku-4-5-20251001"


_SYSTEM_PROMPT = (
    MANAGER_VOICE_RULES
    + "\n"
    + "You produce a JSON array of up to 5 manager recommendations. Each "
    "item has keys: category (one of: coach_rep, run_campaign, "
    "outreach_at_risk_customer, promote_winning_script), title (≤25 "
    "words, plain English), rationale (≤40 words, evidence-cited), "
    "target (object: rep_user_id|customer_id|script_phrase|"
    "campaign_topic depending on category, or empty), evidence (object: "
    "call_count|customer_count|dollar_estimate|sample_ids[] as available), "
    "score (0-100, expected impact). Rank by score descending. Return "
    "ONLY the JSON array, no surrounding prose."
)


def build_for_tenant(session: Session, tenant: Tenant) -> List[ManagerRecommendation]:
    """Generate, sanitize, and insert recommendations for one tenant.

    Returns the inserted rows. Empty list when there isn't enough
    signal to recommend anything (no BusinessProfile, no alerts, etc.).
    """
    profile = _latest_business_profile(session, tenant.id)
    open_alerts = _open_alerts(session, tenant.id)
    if profile is None and not open_alerts:
        return []
    candidates = _candidate_targets(session, tenant.id)

    prompt_body = {
        "tenant_id": str(tenant.id),
        "business_profile": (profile.profile if profile else {}),
        "top_factors": (profile.top_factors if profile else []),
        "open_alerts": [
            {
                "kind": a.kind,
                "severity": a.severity,
                "title": a.title,
                "evidence": a.evidence,
            }
            for a in open_alerts[:20]
        ],
        "playbook_insights": (tenant.tenant_context or {}).get("playbook_insights", {}),
        "candidates": candidates,
    }

    items = _invoke_haiku(prompt_body)
    if not items:
        return []

    inserted: List[ManagerRecommendation] = []
    expires_at = datetime.now(timezone.utc) + timedelta(days=14)
    for item in items:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if category not in {
            "coach_rep",
            "run_campaign",
            "outreach_at_risk_customer",
            "promote_winning_script",
        }:
            continue
        title = item.get("title") or ""
        if not isinstance(title, str) or not title.strip():
            continue
        sanitize_manager_payload(
            item,
            max_words_per_field={"title": 25, "rationale": 40},
            default_max_words=None,
        )
        row = ManagerRecommendation(
            tenant_id=tenant.id,
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


def _invoke_haiku(prompt_body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Single Haiku call returning the parsed JSON array. Empty list on any failure."""
    try:
        client = get_anthropic()
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            temperature=0.0,
            messages=[{"role": "user", "content": json.dumps(prompt_body, default=str)}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()
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
