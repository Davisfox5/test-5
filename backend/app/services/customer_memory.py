"""Customer relationship memory: upsert concerns + their-side
commitments from one interaction's analysis output.

Called once per interaction after the AI analysis pass writes
``interaction.insights``. The analyzer emits two new top-level keys:

* ``concerns_raised`` — list of ``{topic, description, severity,
  sentiment, quote}`` per concern surfaced on the call.
* ``customer_commitments`` — list of ``{description, quote,
  due_date}`` per commitment the customer made (the rep-side
  commitments still flow through ``action_items`` per the existing
  pipeline).

This service handles persistence + lifecycle:

* Concerns upsert per (customer, topic). A negative-sentiment mention
  on a previously-resolved concern moves it to ``monitoring`` first
  (preserving ``resolved_at`` as history); a second negative mention
  escalates ``monitoring`` → ``active``, and only then is the old
  resolution timestamp cleared. A positive-sentiment mention on an
  active concern moves it to ``monitoring``. This two-step reopen
  stops a single offhand remark from flip-flopping the state and
  corrupting the resolution timeline. Severity follows the highest
  recent reading. The ``evidence`` JSONB column accumulates a
  provenance trail.
* Commitments are append-only — they're discrete promises with
  their own due dates, not a state machine on the customer.

Safe to call multiple times for the same interaction; concerns are
idempotent by topic-string, commitments dedupe by exact-description
match within the same customer.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.app.models import (
    CustomerCommitment,
    CustomerConcern,
    Interaction,
    Tenant,
)

logger = logging.getLogger(__name__)


_VALID_SEVERITY = {"high", "medium", "low"}
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
_VALID_SOURCE_MOTION = {
    "sales",
    "customer_service",
    "it_support",
    "generic",
}


def _normalize_topic(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    norm = raw.strip().lower().replace(" ", "_").replace("-", "_")
    norm = "".join(ch for ch in norm if ch.isalnum() or ch == "_")
    return norm[:120] or None


def _coerce_severity(raw: Any) -> str:
    if isinstance(raw, str) and raw.lower() in _VALID_SEVERITY:
        return raw.lower()
    return "medium"


def _coerce_due_date(raw: Any) -> Optional[date]:
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    if isinstance(raw, date):
        return raw
    return None


def update_from_interaction(
    session: Session, interaction: Interaction, insights: Dict[str, Any]
) -> Dict[str, int]:
    """Apply both extractors for one interaction. Returns counts so the
    pipeline log carries the per-row evidence of what changed."""
    if interaction.customer_id is None:
        return {"concerns": 0, "commitments": 0}

    # Concurrent analyses for the same customer (two calls finishing
    # minutes apart) race on the same concern rows; serialize them per
    # (tenant, customer) for the rest of this transaction. Postgres
    # only — the SQLite test fixture runs single-writer anyway.
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"cust-mem:{interaction.tenant_id}:{interaction.customer_id}"},
        )

    motion = interaction.domain if isinstance(interaction.domain, str) else None
    if motion not in _VALID_SOURCE_MOTION:
        motion = None

    concerns_raised = insights.get("concerns_raised")
    customer_commitments = insights.get("customer_commitments")

    concerns_n = _upsert_concerns(
        session,
        interaction=interaction,
        motion=motion,
        items=concerns_raised if isinstance(concerns_raised, list) else [],
    )
    commitments_n = _insert_commitments(
        session,
        interaction=interaction,
        items=customer_commitments if isinstance(customer_commitments, list) else [],
    )
    return {"concerns": concerns_n, "commitments": commitments_n}


# ── Concerns ───────────────────────────────────────────────────────────


def _upsert_concerns(
    session: Session,
    *,
    interaction: Interaction,
    motion: Optional[str],
    items: Iterable[Any],
) -> int:
    """Upsert concerns from the analyzer's ``concerns_raised`` list.

    For each item with a valid topic:

    * If a concern row exists for (customer, topic), update
      last-seen, append evidence, possibly bump severity, transition
      status based on the new mention's sentiment.
    * Otherwise insert a fresh row with status='active'.
    """
    now = datetime.now(timezone.utc)
    changed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        topic = _normalize_topic(item.get("topic"))
        if not topic:
            continue
        severity = _coerce_severity(item.get("severity"))
        description = item.get("description")
        if not isinstance(description, str):
            description = None
        quote = item.get("quote") if isinstance(item.get("quote"), str) else None
        sentiment = (
            item.get("sentiment").lower()
            if isinstance(item.get("sentiment"), str)
            else "negative"
        )

        existing = (
            session.execute(
                select(CustomerConcern).where(
                    CustomerConcern.tenant_id == interaction.tenant_id,
                    CustomerConcern.customer_id == interaction.customer_id,
                    CustomerConcern.topic == topic,
                )
            )
        ).scalar_one_or_none()

        evidence_item: Dict[str, Any] = {
            "interaction_id": str(interaction.id),
            "occurred_at": (interaction.created_at or now).isoformat()
            if interaction.created_at
            else now.isoformat(),
            "sentiment": sentiment,
            "motion": motion,
        }
        if quote:
            evidence_item["quote"] = quote[:1000]

        if existing is None:
            row = CustomerConcern(
                tenant_id=interaction.tenant_id,
                customer_id=interaction.customer_id,
                topic=topic,
                description=description,
                severity=severity,
                source_motion=motion,
                first_seen_interaction_id=interaction.id,
                last_seen_interaction_id=interaction.id,
                first_seen_at=interaction.created_at or now,
                last_seen_at=interaction.created_at or now,
                evidence=[evidence_item],
            )
            session.add(row)
            changed += 1
            continue

        # Existing concern: append evidence, possibly transition.
        existing.last_seen_interaction_id = interaction.id
        if interaction.created_at:
            existing.last_seen_at = interaction.created_at
        # Bump severity if this mention came in higher.
        if _SEVERITY_RANK.get(severity, 1) > _SEVERITY_RANK.get(existing.severity, 1):
            existing.severity = severity
        # Description: only fill if we didn't have one yet.
        if description and not existing.description:
            existing.description = description
        # Status transition based on sentiment of the new mention.
        before_status = existing.status
        if sentiment == "positive":
            if existing.status == "active":
                existing.status = "monitoring"
                existing.status_changed_at = now
        elif sentiment == "negative":
            if before_status in ("monitoring", "dormant"):
                existing.status = "active"
                existing.status_changed_at = now
                # Fully reactivated: the prior resolution (if any) is
                # genuinely over, so the timestamp stops being history.
                existing.resolved_at = None
            elif before_status == "resolved":
                # One mention after resolution watches, it doesn't
                # reopen — ``resolved_at`` stays intact so the timeline
                # survives an offhand remark. A second negative mention
                # escalates via the branch above.
                existing.status = "monitoring"
                existing.status_changed_at = now
        # 'neutral' or 'mixed' don't move the status by themselves.
        ev = list(existing.evidence or [])
        ev.append(evidence_item)
        # Keep evidence bounded — 50 most recent mentions is plenty
        # for the UI; older mentions stay in the analyzer's raw output.
        if len(ev) > 50:
            ev = ev[-50:]
        existing.evidence = ev
        changed += 1
    if changed:
        session.flush()
    return changed


# ── Commitments (their side) ───────────────────────────────────────────


def _insert_commitments(
    session: Session,
    *,
    interaction: Interaction,
    items: Iterable[Any],
) -> int:
    """Append-only insert of customer-side commitments.

    Dedupes against existing rows for the same customer with the same
    description so a re-analysis of the same interaction doesn't fan
    out duplicates. ``due_date`` is parsed best-effort.
    """
    inserted = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            continue
        description = description.strip()
        quote = item.get("quote") if isinstance(item.get("quote"), str) else None
        due = _coerce_due_date(item.get("due_date"))

        existing = (
            session.execute(
                select(CustomerCommitment.id).where(
                    CustomerCommitment.tenant_id == interaction.tenant_id,
                    CustomerCommitment.customer_id == interaction.customer_id,
                    CustomerCommitment.description == description,
                )
            )
        ).first()
        if existing is not None:
            continue
        row = CustomerCommitment(
            tenant_id=interaction.tenant_id,
            customer_id=interaction.customer_id,
            source_interaction_id=interaction.id,
            description=description[:2000],
            quote=quote[:2000] if quote else None,
            due_date=due,
        )
        session.add(row)
        inserted += 1
    if inserted:
        session.flush()
    return inserted


# ── Background sweep: stale active → dormant ───────────────────────────


DORMANT_AFTER_DAYS = 90


def sweep_dormant_concerns(
    session: Session, *, now: Optional[datetime] = None
) -> int:
    """Transition active/monitoring concerns to ``dormant`` after they
    haven't been mentioned in ``DORMANT_AFTER_DAYS``. Runs as part of
    the nightly task chain so a concern doesn't sit perpetually
    ``active`` after the customer moved on."""
    from datetime import timedelta

    from backend.app.tenant_ctx import tenant_context

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DORMANT_AFTER_DAYS)
    transitioned = 0
    for tenant in session.execute(select(Tenant)).scalars().all():
        with tenant_context(tenant.id, session):
            rows = (
                session.execute(
                    select(CustomerConcern).where(
                        CustomerConcern.tenant_id == tenant.id,
                        CustomerConcern.status.in_(("active", "monitoring")),
                        CustomerConcern.last_seen_at < cutoff,
                    )
                )
                .scalars()
                .all()
            )
            for r in rows:
                # Compare in the same tz shape as the row stores (SQLite tests
                # drop tz; Postgres keeps it). Helper avoids the
                # naive-vs-aware compare blow-up the QBR scanner had.
                last = r.last_seen_at
                if last is not None and last.tzinfo is None:
                    cmp_cutoff = cutoff.replace(tzinfo=None)
                else:
                    cmp_cutoff = cutoff
                if last is None or last < cmp_cutoff:
                    r.status = "dormant"
                    r.status_changed_at = now
            transitioned += len(rows)
    if transitioned:
        session.flush()
    return transitioned
