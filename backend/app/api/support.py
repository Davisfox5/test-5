"""IT-Support case REST endpoints.

Surfaces ``SupportCase`` CRUD for the support-agent and support-manager
portals, plus the helper routes for status transitions, assignment, and
CSAT capture. Routes are gated on ``require_domain_agent("it_support")``
for create/list/update; tenant admins and support managers also pass.

Detail / list shapes include the linked interaction timeline so the
case-detail page can render without a second round-trip per case.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    require_domain_agent,
)
from backend.app.db import get_db
from backend.app.models import Customer, Interaction, SupportCase, User

logger = logging.getLogger(__name__)

router = APIRouter()


_OPEN_STATES = {"open", "in_progress", "escalated"}
_STATUSES = ("open", "in_progress", "escalated", "resolved", "closed")
_PRIORITIES = ("high", "medium", "low")


# ── Pydantic shapes ────────────────────────────────────────────────────


class SupportCaseSummary(BaseModel):
    """Row shape on the case-queue table."""

    id: uuid.UUID
    subject: str
    status: str
    priority: str
    assigned_to: Optional[uuid.UUID]
    assigned_to_name: Optional[str]
    customer_id: Optional[uuid.UUID]
    customer_name: Optional[str]
    opened_at: datetime
    first_response_at: Optional[datetime]
    escalated_at: Optional[datetime]
    resolved_at: Optional[datetime]
    closed_at: Optional[datetime]
    csat_score: Optional[int]
    first_contact_resolution: Optional[bool]
    interaction_count: int


class SupportCaseInteractionRow(BaseModel):
    """Tiny interaction summary on the case-detail page timeline."""

    id: uuid.UUID
    channel: str
    direction: Optional[str]
    title: Optional[str]
    created_at: datetime


class SupportCaseDetail(SupportCaseSummary):
    description: Optional[str]
    category: Optional[str]
    interactions: List[SupportCaseInteractionRow]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SupportCaseCreateIn(BaseModel):
    customer_id: Optional[uuid.UUID] = None
    subject: str = Field(..., min_length=1, max_length=300)
    description: Optional[str] = None
    priority: Literal["high", "medium", "low"] = "medium"
    category: Optional[str] = None


class SupportCaseStatusIn(BaseModel):
    status: Literal["open", "in_progress", "escalated", "resolved", "closed"]


class SupportCasePriorityIn(BaseModel):
    priority: Literal["high", "medium", "low"]


class SupportCaseAssignIn(BaseModel):
    """``user_id=null`` unassigns. Otherwise the user must be a tenant
    member with ``it_support`` in their agent or manager domains."""

    user_id: Optional[uuid.UUID]


class SupportCaseLinkIn(BaseModel):
    interaction_id: uuid.UUID


class CsatIn(BaseModel):
    score: int = Field(..., ge=1, le=5)


# ── Helpers ────────────────────────────────────────────────────────────


async def _load_case_for_tenant(
    db: AsyncSession, tenant_id: uuid.UUID, case_id: uuid.UUID
) -> SupportCase:
    case = await db.get(SupportCase, case_id)
    if case is None or case.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


async def _interaction_count(
    db: AsyncSession, case_id: uuid.UUID
) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Interaction)
                .where(Interaction.support_case_id == case_id)
            )
        ).scalar_one()
    )


async def _summarize(
    db: AsyncSession, case: SupportCase
) -> SupportCaseSummary:
    customer_name: Optional[str] = None
    if case.customer_id is not None:
        c = await db.get(Customer, case.customer_id)
        customer_name = c.name if c is not None else None
    assignee_name: Optional[str] = None
    if case.assigned_to is not None:
        u = await db.get(User, case.assigned_to)
        assignee_name = (u.name or u.email) if u is not None else None
    return SupportCaseSummary(
        id=case.id,
        subject=case.subject,
        status=case.status,
        priority=case.priority,
        assigned_to=case.assigned_to,
        assigned_to_name=assignee_name,
        customer_id=case.customer_id,
        customer_name=customer_name,
        opened_at=case.opened_at,
        first_response_at=case.first_response_at,
        escalated_at=case.escalated_at,
        resolved_at=case.resolved_at,
        closed_at=case.closed_at,
        csat_score=case.csat_score,
        first_contact_resolution=case.first_contact_resolution,
        interaction_count=await _interaction_count(db, case.id),
    )


# ── Routes ─────────────────────────────────────────────────────────────


@router.get(
    "/support/cases",
    response_model=List[SupportCaseSummary],
)
async def list_cases(
    status: Optional[str] = Query(
        None,
        description=(
            "Filter by lifecycle. ``open_all`` (default behaviour when "
            "omitted) returns open + in_progress + escalated; otherwise "
            "an exact match on one status."
        ),
    ),
    assigned_to: Optional[uuid.UUID] = Query(
        None, description="Filter to a specific assignee."
    ),
    mine_only: bool = Query(
        False,
        description="Convenience: filter to cases assigned to the caller.",
    ),
    priority: Optional[Literal["high", "medium", "low"]] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("it_support")),
) -> List[SupportCaseSummary]:
    stmt = select(SupportCase).where(SupportCase.tenant_id == principal.tenant.id)
    if status is None or status == "open_all":
        stmt = stmt.where(SupportCase.status.in_(tuple(_OPEN_STATES)))
    else:
        if status not in _STATUSES:
            raise HTTPException(status_code=422, detail=f"Unknown status: {status}")
        stmt = stmt.where(SupportCase.status == status)
    if mine_only and principal.user_id is not None:
        stmt = stmt.where(SupportCase.assigned_to == principal.user_id)
    elif assigned_to is not None:
        stmt = stmt.where(SupportCase.assigned_to == assigned_to)
    if priority is not None:
        stmt = stmt.where(SupportCase.priority == priority)
    stmt = stmt.order_by(desc(SupportCase.opened_at)).limit(limit)
    cases = (await db.execute(stmt)).scalars().all()
    return [await _summarize(db, c) for c in cases]


@router.post(
    "/support/cases",
    response_model=SupportCaseDetail,
    status_code=201,
)
async def create_case(
    body: SupportCaseCreateIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("it_support")),
) -> SupportCaseDetail:
    case = SupportCase(
        tenant_id=principal.tenant.id,
        customer_id=body.customer_id,
        subject=body.subject,
        description=body.description,
        category=body.category,
        status="open",
        priority=body.priority,
    )
    db.add(case)
    await db.flush()
    # Background embed so the daily trend scan doesn't have to spike-
    # embed every backlogged case at 07:00 UTC. Falls back to the
    # daily scan's missing-embedding query if Celery is unreachable.
    try:
        from backend.app.tasks import embed_support_case_subject

        embed_support_case_subject.delay(str(case.id))
    except Exception:
        pass
    try:
        from backend.app.services.webhook_dispatcher import emit_event

        await emit_event(
            db, principal.tenant.id, "support_case.opened",
            {
                "case_id": str(case.id),
                "customer_id": str(case.customer_id) if case.customer_id else None,
                "subject": case.subject,
                "priority": case.priority,
                "category": case.category,
            },
        )
    except Exception:
        logger.exception("emit support_case.opened webhook failed for %s", case.id)
    return await _detail(db, case)


async def _detail(
    db: AsyncSession, case: SupportCase
) -> SupportCaseDetail:
    summary = await _summarize(db, case)
    interactions = (
        await db.execute(
            select(
                Interaction.id,
                Interaction.channel,
                Interaction.direction,
                Interaction.title,
                Interaction.created_at,
            )
            .where(Interaction.support_case_id == case.id)
            .order_by(Interaction.created_at.asc())
        )
    ).all()
    rows = [
        SupportCaseInteractionRow(
            id=r[0],
            channel=r[1],
            direction=r[2],
            title=r[3],
            created_at=r[4],
        )
        for r in interactions
    ]
    return SupportCaseDetail(
        **summary.model_dump(),
        description=case.description,
        category=case.category,
        metadata=case.metadata_ or {},
        interactions=rows,
    )


@router.get(
    "/support/cases/{case_id}",
    response_model=SupportCaseDetail,
)
async def get_case(
    case_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("it_support")),
) -> SupportCaseDetail:
    case = await _load_case_for_tenant(db, principal.tenant.id, case_id)
    return await _detail(db, case)


@router.post(
    "/support/cases/{case_id}/status",
    response_model=SupportCaseDetail,
)
async def transition_case(
    case_id: uuid.UUID,
    body: SupportCaseStatusIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("it_support")),
) -> SupportCaseDetail:
    case = await _load_case_for_tenant(db, principal.tenant.id, case_id)
    now = datetime.now(timezone.utc)
    before_status = case.status
    case.status = body.status
    if body.status == "escalated" and case.escalated_at is None:
        case.escalated_at = now
    elif body.status == "resolved" and case.resolved_at is None:
        case.resolved_at = now
        ic = await _interaction_count(db, case.id)
        if case.first_contact_resolution is None:
            case.first_contact_resolution = ic <= 1
    elif body.status == "closed" and case.closed_at is None:
        case.closed_at = now
    await db.flush()
    # Cross-motion notification: fire ``case_escalated`` to the support
    # managers when a case enters the escalated state for the first
    # time. Best-effort — failures don't block the transition.
    if (
        body.status == "escalated"
        and before_status != "escalated"
    ):
        await _notify_case_escalated(db, principal, case)
    if before_status != case.status:
        try:
            from backend.app.services.webhook_dispatcher import emit_event

            await emit_event(
                db, principal.tenant.id, "support_case.status_changed",
                {
                    "case_id": str(case.id),
                    "customer_id": str(case.customer_id) if case.customer_id else None,
                    "subject": case.subject,
                    "old_status": before_status,
                    "new_status": case.status,
                },
            )
        except Exception:
            logger.exception(
                "emit support_case.status_changed webhook failed for %s", case.id,
            )
    return await _detail(db, case)


async def _notify_case_escalated(
    db: AsyncSession,
    principal: AuthPrincipal,
    case: SupportCase,
) -> None:
    """Fire a ``case_escalated`` notification to every support manager
    in the tenant. Recipient resolution intentionally broad: a single
    case_escalated event is rare enough that paging the support manager
    pool is the right default. Volume-sensitive customers can move to
    digest mode later."""
    from backend.app.services.notifications import (
        NotificationKind,
        notify,
    )

    recipients = (
        await db.execute(
            select(User.id).where(
                User.tenant_id == principal.tenant.id,
                User.is_active.is_(True),
                User.manager_domains.contains(["it_support"]),  # JSONB contains
            )
        )
    ).all()
    customer_name = ""
    if case.customer_id is not None:
        c = await db.get(Customer, case.customer_id)
        customer_name = (c.name if c else "") or ""
    title = (
        f"Case escalated: {case.subject[:80]}"
        if customer_name == ""
        else f"Case escalated ({customer_name}): {case.subject[:80]}"
    )
    for (uid,) in recipients:
        await notify(
            db,
            tenant_id=principal.tenant.id,
            user_id=uid,
            kind=NotificationKind.CASE_ESCALATED,
            title=title,
            body=case.subject,
            link_url=f"/support/cases/{case.id}",
        )


@router.post(
    "/support/cases/{case_id}/assign",
    response_model=SupportCaseDetail,
)
async def assign_case(
    case_id: uuid.UUID,
    body: SupportCaseAssignIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("it_support")),
) -> SupportCaseDetail:
    case = await _load_case_for_tenant(db, principal.tenant.id, case_id)
    before_assignee = case.assigned_to
    if body.user_id is None:
        case.assigned_to = None
    else:
        user = await db.get(User, body.user_id)
        if user is None or user.tenant_id != principal.tenant.id:
            raise HTTPException(status_code=404, detail="Assignee not found")
        # Don't assign cases to users outside the IT-support motion;
        # tenant admins always pass.
        agent_domains = user.agent_domains or []
        manager_domains = user.manager_domains or []
        if not (
            user.is_tenant_admin
            or "it_support" in agent_domains
            or "it_support" in manager_domains
        ):
            raise HTTPException(
                status_code=400,
                detail="Assignee doesn't have IT Support access.",
            )
        case.assigned_to = body.user_id
    await db.flush()
    # Cross-motion notification: only fire on real assignment changes
    # (skip when assigning to the same person or unassigning).
    if (
        body.user_id is not None
        and body.user_id != before_assignee
        and body.user_id != principal.user_id  # don't notify self-assigns
    ):
        from backend.app.services.notifications import (
            NotificationKind,
            notify,
        )

        customer_name = ""
        if case.customer_id is not None:
            c = await db.get(Customer, case.customer_id)
            customer_name = (c.name if c else "") or ""
        title = (
            f"Case assigned: {case.subject[:80]}"
            if not customer_name
            else f"Case assigned ({customer_name}): {case.subject[:80]}"
        )
        await notify(
            db,
            tenant_id=principal.tenant.id,
            user_id=body.user_id,
            kind=NotificationKind.CASE_ASSIGNED,
            title=title,
            body=case.subject,
            link_url=f"/support/cases/{case.id}",
        )
    if case.assigned_to != before_assignee:
        try:
            from backend.app.services.webhook_dispatcher import emit_event

            await emit_event(
                db, principal.tenant.id, "support_case.assigned",
                {
                    "case_id": str(case.id),
                    "customer_id": str(case.customer_id) if case.customer_id else None,
                    "subject": case.subject,
                    "old_assignee_id": str(before_assignee) if before_assignee else None,
                    "new_assignee_id": str(case.assigned_to) if case.assigned_to else None,
                },
            )
        except Exception:
            logger.exception(
                "emit support_case.assigned webhook failed for %s", case.id,
            )
    return await _detail(db, case)


@router.post(
    "/support/cases/{case_id}/priority",
    response_model=SupportCaseDetail,
)
async def set_priority(
    case_id: uuid.UUID,
    body: SupportCasePriorityIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("it_support")),
) -> SupportCaseDetail:
    case = await _load_case_for_tenant(db, principal.tenant.id, case_id)
    case.priority = body.priority
    await db.flush()
    return await _detail(db, case)


@router.post(
    "/support/cases/{case_id}/csat",
    response_model=SupportCaseDetail,
)
async def record_csat(
    case_id: uuid.UUID,
    body: CsatIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("it_support")),
) -> SupportCaseDetail:
    """Internal CSAT capture (e.g. agent recording the customer's phone
    response). The public web form uses ``/csat/<token>`` instead — no
    auth, signed token."""
    case = await _load_case_for_tenant(db, principal.tenant.id, case_id)
    if case.status not in ("resolved", "closed"):
        raise HTTPException(
            status_code=400,
            detail="CSAT can only be recorded after a case is resolved or closed.",
        )
    case.csat_score = body.score
    await db.flush()
    return await _detail(db, case)


@router.post(
    "/support/cases/{case_id}/link",
    response_model=SupportCaseDetail,
)
async def link_interaction(
    case_id: uuid.UUID,
    body: SupportCaseLinkIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("it_support")),
) -> SupportCaseDetail:
    """Attach an existing interaction to a case (manual link from the
    inbox when the auto-attach heuristic missed)."""
    case = await _load_case_for_tenant(db, principal.tenant.id, case_id)
    ix = await db.get(Interaction, body.interaction_id)
    if ix is None or ix.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Interaction not found")
    ix.support_case_id = case.id
    await db.flush()
    return await _detail(db, case)


# ── Token issuance (admin-side: agent fetches the public CSAT URL) ────


class CsatTokenOut(BaseModel):
    token: str
    public_url: str


@router.post(
    "/support/cases/{case_id}/csat-token",
    response_model=CsatTokenOut,
)
async def issue_case_csat_token(
    case_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("it_support")),
) -> CsatTokenOut:
    """Mint the signed token agents paste into outbound CSAT emails.

    Customer follows ``<frontend_url>/csat/<token>`` and submits a
    1-5 score. The token is HMAC-signed with the tenant's
    ``csat_token_secret``; the public form route validates and writes
    back. Doesn't require the case to be resolved yet — agents
    sometimes want the survey link ready before they hit "resolve".
    """
    from backend.app.config import get_settings
    from backend.app.services.support_case_service import issue_csat_token

    case = await _load_case_for_tenant(db, principal.tenant.id, case_id)
    settings = get_settings()
    secret = (
        principal.tenant.outcomes_hmac_secret
        or settings.SESSION_JWT_SECRET
        or ""
    )
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="No CSAT signing secret configured for this tenant.",
        )
    token = issue_csat_token(case, secret=secret)
    public_url = f"/csat/{token}"
    return CsatTokenOut(token=token, public_url=public_url)
