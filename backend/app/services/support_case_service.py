"""SupportCase lifecycle + auto-create-or-attach logic.

Used by the interaction-ingestion pipeline (``backend/app/tasks.py``)
when a new interaction lands with ``domain='it_support'``: this service
decides whether the interaction is a new ticket or a follow-up on an
existing open case, links the FK, and stamps lifecycle timestamps.

Dedupe heuristic: an inbound IT-support interaction attaches to the
most-recent open case for the same customer if that case is no more
than ``OPEN_WINDOW_HOURS`` old. Otherwise a new case is opened.
Conservative window because attaching the wrong interaction to an
existing case is harder to undo than splitting a case later.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Interaction, SupportCase

logger = logging.getLogger(__name__)


# A new interaction attaches to an existing open case for the same
# customer only when that case is fresher than this many hours.
# 7 days is the default — long enough that a multi-touch issue stays
# linked, short enough that an unrelated follow-up months later opens
# a new ticket.
OPEN_WINDOW_HOURS = 24 * 7

# Lifecycle states that count as "still actively worked." A case in
# resolved or closed cannot be auto-attached to.
_OPEN_STATES = ("open", "in_progress", "escalated")


def attach_or_create_case(
    session: Session,
    interaction: Interaction,
) -> Optional[SupportCase]:
    """Open or attach a SupportCase for an inbound IT-support interaction.

    Returns the case (newly-created or re-used) with
    ``interaction.support_case_id`` set. Returns None when the
    interaction has no usable customer linkage — without a customer we
    can't dedupe, so we leave the FK NULL rather than create a
    customer-less case.
    """
    if interaction.domain != "it_support":
        return None
    if interaction.support_case_id is not None:
        return session.get(SupportCase, interaction.support_case_id)
    if interaction.customer_id is None:
        # No customer to dedupe against. We could still open an orphan
        # case here, but those are awkward to manage in the queue UI
        # (no "account" column) and the next interaction will rarely
        # have any way to link back. Skip; the manager can manually
        # create one from the inbox if needed.
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(hours=OPEN_WINDOW_HOURS)
    existing = (
        session.execute(
            select(SupportCase)
            .where(
                SupportCase.tenant_id == interaction.tenant_id,
                SupportCase.customer_id == interaction.customer_id,
                SupportCase.status.in_(_OPEN_STATES),
                SupportCase.opened_at >= cutoff,
            )
            .order_by(SupportCase.opened_at.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )

    if existing is not None:
        interaction.support_case_id = existing.id
        # Stamp ``first_response_at`` the first time an agent-side
        # interaction (anything other than the customer's initial
        # contact) attaches to the case. Best-effort: we don't have a
        # reliable per-tenant "first response" definition, so this
        # populates on the second interaction on the case.
        if existing.first_response_at is None:
            prior_count = (
                session.execute(
                    select(Interaction.id)
                    .where(Interaction.support_case_id == existing.id)
                )
                .all()
            )
            if len(prior_count) >= 1:
                existing.first_response_at = datetime.now(timezone.utc)
        session.flush()
        logger.info(
            "Attached interaction %s to existing case %s (customer=%s)",
            interaction.id, existing.id, interaction.customer_id,
        )
        return existing

    # New case. Subject heuristic: the interaction's ``title`` if set,
    # otherwise the first ~80 chars of the transcript body, otherwise
    # a generic placeholder. The agent-side UI lets the support agent
    # rename the case so this is just a first-render label.
    subject = (interaction.title or "").strip()
    if not subject:
        raw = (interaction.raw_text or "").strip()
        if raw:
            subject = raw[:80].rsplit(" ", 1)[0]
    if not subject:
        subject = "New support case"

    case = SupportCase(
        tenant_id=interaction.tenant_id,
        customer_id=interaction.customer_id,
        subject=subject[:300],
        status="open",
        priority="medium",
    )
    session.add(case)
    session.flush()
    interaction.support_case_id = case.id
    session.flush()
    logger.info(
        "Opened new case %s from interaction %s (customer=%s)",
        case.id, interaction.id, interaction.customer_id,
    )
    # Fire-and-forget background embed so the daily trend scan doesn't
    # have to spike-embed every backlogged case at 07:00 UTC. Case
    # creation stays fast (we don't await Voyage on the hot path); the
    # async task picks up the row and writes ``subject_embedding`` +
    # ``embedded_at`` once it lands. If Celery is unreachable the daily
    # scan's TTL+missing-embedding query will still pick it up as a
    # fallback.
    try:
        from backend.app.tasks import embed_support_case_subject

        embed_support_case_subject.delay(str(case.id))
    except Exception:
        logger.debug(
            "Failed to enqueue background embed for support case %s", case.id,
            exc_info=True,
        )
    return case


def transition_status(
    session: Session,
    case: SupportCase,
    *,
    next_status: str,
    now: Optional[datetime] = None,
) -> SupportCase:
    """Move a case through its lifecycle.

    Stamps the transition-specific timestamp (``escalated_at``,
    ``resolved_at``, ``closed_at``) so the detector and SLA reports
    have ground truth without having to mine the audit log.

    ``first_contact_resolution`` is stamped on the transition to
    ``resolved`` when the case has exactly one linked interaction —
    i.e. the agent resolved on first contact. Re-resolving an already-
    resolved case (e.g. after a customer reply that turned out not to
    need anything) doesn't reset the flag.
    """
    if next_status not in ("open", "in_progress", "escalated", "resolved", "closed"):
        raise ValueError(f"Unknown SupportCase status: {next_status}")
    now = now or datetime.now(timezone.utc)
    before = case.status
    case.status = next_status

    if next_status == "escalated" and case.escalated_at is None:
        case.escalated_at = now
    elif next_status == "resolved" and case.resolved_at is None:
        case.resolved_at = now
        interaction_count = (
            session.execute(
                select(Interaction.id).where(
                    Interaction.support_case_id == case.id
                )
            )
        ).all()
        if case.first_contact_resolution is None:
            case.first_contact_resolution = len(interaction_count) <= 1
    elif next_status == "closed" and case.closed_at is None:
        case.closed_at = now
        # Closing without resolving is allowed (the agent gave up; the
        # customer went quiet) — don't stamp resolved_at retroactively.

    session.flush()
    logger.info(
        "Case %s transitioned %s -> %s",
        case.id, before, next_status,
    )
    return case


def record_csat(
    session: Session,
    case: SupportCase,
    *,
    score: int,
) -> SupportCase:
    """Write a 1-5 CSAT score on a resolved/closed case.

    Rejects updates on cases that haven't reached resolved/closed yet
    — collecting CSAT while a case is still being worked is misleading.
    Rejects scores outside 1-5 to match the model's CHECK constraint.
    """
    if score < 1 or score > 5:
        raise ValueError("CSAT must be 1-5")
    if case.status not in ("resolved", "closed"):
        raise ValueError(
            "CSAT can only be recorded after a case is resolved or closed"
        )
    case.csat_score = score
    session.flush()
    return case


# ── Token helpers for the public CSAT form ──────────────────────────────


def issue_csat_token(case: SupportCase, *, secret: str) -> str:
    """Mint a signed token the customer can use on the public CSAT form.

    Format: ``<case_id_hex>.<hmac_sha256(secret, case_id_hex)[:16]>``.
    Short HMAC slice keeps the URL short; full case_id remains in the
    payload so the route can look it up. Forging a valid token requires
    the per-tenant ``CSAT_TOKEN_SECRET`` — the customer can't enumerate
    other cases.

    Tokens don't carry an expiry; rate-limiting the public route is
    the right layer to throttle replay attempts.
    """
    import hashlib
    import hmac

    cid = case.id.hex
    sig = hmac.new(secret.encode("utf-8"), cid.encode("ascii"), hashlib.sha256)
    return f"{cid}.{sig.hexdigest()[:16]}"


def verify_csat_token(token: str, *, secret: str) -> Optional[uuid.UUID]:
    """Validate a CSAT token and return the case id, or None on tamper.

    Constant-time compare on the signature slice so a forged token
    can't be brute-forced byte-by-byte via response timing.
    """
    import hashlib
    import hmac

    if "." not in token:
        return None
    cid_str, presented_sig = token.split(".", 1)
    if len(presented_sig) != 16:
        return None
    try:
        case_id = uuid.UUID(cid_str)
    except ValueError:
        return None
    expected = hmac.new(
        secret.encode("utf-8"), cid_str.encode("ascii"), hashlib.sha256
    ).hexdigest()[:16]
    if not hmac.compare_digest(expected, presented_sig):
        return None
    return case_id
