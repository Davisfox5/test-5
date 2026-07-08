"""Deterministic broken-commitment detector. No LLM involved.

``CustomerCommitment`` (``backend/app/models.py``) is append-only: a
customer-side promise with a ``due_date`` and, once fulfilled, a
``met_at``. A commitment is "broken" purely by the clock — ``due_date``
has passed and ``met_at`` is still NULL. That's a deterministic SQL
predicate, not a judgment call, so this detector runs no analysis at
all: it just flags what the calendar already says.

Each broken commitment gets its row's ``status`` flipped to ``broken``
(so a re-scan doesn't keep re-selecting it) and one ``ManagerAlert``
(deduped by a fingerprint on the commitment id, as a defense-in-depth
belt-and-suspenders alongside the status flip) so a CSM sees "they said
X by Friday, still hasn't happened" without waiting on any AI call.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Customer, CustomerCommitment, ManagerAlert, Tenant

logger = logging.getLogger(__name__)

ALERT_KIND = "broken_commitment_detected"


def _fingerprint(commitment_id: Any) -> str:
    return hashlib.sha256(
        f"{ALERT_KIND}::{commitment_id}".encode("utf-8")
    ).hexdigest()[:32]


def detect_and_flag(
    session: Session, tenant: Tenant, *, today: Optional[date] = None
) -> Dict[str, int]:
    """Flag every ``open`` commitment whose ``due_date`` has passed with
    no ``met_at``. Returns ``{"scanned": N, "flagged": M}``."""
    today = today or datetime.now(timezone.utc).date()
    rows = (
        session.execute(
            select(CustomerCommitment).where(
                CustomerCommitment.tenant_id == tenant.id,
                CustomerCommitment.status == "open",
                CustomerCommitment.due_date.isnot(None),
                CustomerCommitment.due_date < today,
                CustomerCommitment.met_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {"scanned": 0, "flagged": 0}

    flagged = 0
    for commitment in rows:
        commitment.status = "broken"
        fingerprint = _fingerprint(commitment.id)
        existing = session.execute(
            select(ManagerAlert.id).where(
                ManagerAlert.tenant_id == tenant.id,
                ManagerAlert.fingerprint == fingerprint,
                ManagerAlert.resolved_at.is_(None),
            )
        ).first()
        if existing is not None:
            continue
        cust = session.get(Customer, commitment.customer_id)
        cust_name = cust.name if cust is not None else "A customer"
        days_overdue = (today - commitment.due_date).days
        alert = ManagerAlert(
            tenant_id=tenant.id,
            kind=ALERT_KIND,
            severity="high" if days_overdue > 14 else "medium",
            title=f"{cust_name} hasn't followed through on a commitment",
            body=(
                f'{cust_name} said: "{commitment.description[:200]}" '
                f"— due {days_overdue} day{'s' if days_overdue != 1 else ''} "
                "ago, still open."
            ),
            evidence={
                "commitment_id": str(commitment.id),
                "description": commitment.description,
                "due_date": commitment.due_date.isoformat(),
                "days_overdue": days_overdue,
            },
            fingerprint=fingerprint,
            domain="customer_service",
        )
        session.add(alert)
        flagged += 1
    session.flush()
    session.commit()
    return {"scanned": len(rows), "flagged": flagged}
