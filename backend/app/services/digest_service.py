"""Vocabulary digest — weekly summary email/Slack of pending candidates.

Wired into the existing notification webhooks (Slack via tenant feature flag).
The actual delivery is best-effort and logs on failure — this is admin tooling
and shouldn't block other improvement loops.

Per the plan this is Gate 1 (vocabulary promotion).  Tenant admins can also
just hit ``/api/v1/analytics/vocabulary-pending`` and approve via the
existing ``/api/v1/evaluation/vocabulary/{id}/approve`` endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from backend.app.models import Tenant, VocabularyCandidate
from backend.app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


def _candidates_for_tenant(session: Session, tenant_id: Any, limit: int = 25) -> List[VocabularyCandidate]:
    return (
        session.query(VocabularyCandidate)
        .filter(
            VocabularyCandidate.tenant_id == tenant_id,
            VocabularyCandidate.status == "pending",
        )
        .order_by(VocabularyCandidate.occurrence_count.desc())
        .limit(limit)
        .all()
    )


def _format_message(tenant: Tenant, candidates: List[VocabularyCandidate]) -> str:
    lines = [f"*{tenant.name} — pending vocabulary candidates ({len(candidates)})*"]
    for c in candidates:
        lines.append(
            f"• `{c.term}` — {c.confidence} (×{c.occurrence_count}) source={c.source or 'unknown'}"
        )
    lines.append(
        "\nReview at /demo.html#analytics or POST "
        "/api/v1/evaluation/vocabulary/{id}/approve to accept."
    )
    return "\n".join(lines)


def send_vocabulary_digests(session: Session) -> Dict[str, Any]:
    """Iterate every tenant with a configured Slack webhook + pending candidates."""
    tenants = session.query(Tenant).all()
    notif = NotificationService()
    sent = 0
    for tenant in tenants:
        webhook = (tenant.features_enabled or {}).get("slack_vocab_digest_webhook")
        if not webhook:
            continue
        cands = _candidates_for_tenant(session, tenant.id)
        if not cands:
            continue
        try:
            asyncio.run(notif.notify_slack(webhook, _format_message(tenant, cands)))
            sent += 1
        except Exception:
            logger.exception("Vocab digest send failed for tenant %s", tenant.id)
    return {"tenants_notified": sent}
