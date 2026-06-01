"""Customer relationship memory API.

Surfaces the per-customer profile that LINDA builds across the whole
relationship: tracked concerns + their-side commitments, plus the
controls agents need to override status manually (resolve a concern
the analyzer missed, mark a customer commitment as met).

Gated on tenant membership — any authenticated user in the tenant
can read; only ``manager_domains`` holders + tenant admins can patch.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    get_current_tenant,
)
from backend.app.db import get_db
from backend.app.models import (
    Customer,
    CustomerCommitment,
    CustomerConcern,
    Tenant,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic shapes ────────────────────────────────────────────────────


class ConcernOut(BaseModel):
    id: uuid.UUID
    topic: str
    description: Optional[str]
    status: str
    severity: str
    source_motion: Optional[str]
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: Optional[datetime]
    status_changed_at: datetime
    evidence_count: int


class CommitmentOut(BaseModel):
    id: uuid.UUID
    description: str
    quote: Optional[str]
    due_date: Optional[date]
    status: str
    met_at: Optional[datetime]
    source_interaction_id: Optional[uuid.UUID]
    created_at: datetime


class CustomerMemoryOut(BaseModel):
    customer_id: uuid.UUID
    customer_name: str
    concerns: List[ConcernOut]
    commitments: List[CommitmentOut]


class ConcernPatchIn(BaseModel):
    status: Optional[Literal["active", "monitoring", "resolved", "dormant"]] = None
    severity: Optional[Literal["high", "medium", "low"]] = None
    description: Optional[str] = None


class CommitmentPatchIn(BaseModel):
    status: Optional[Literal["open", "met", "broken", "dismissed"]] = None
    description: Optional[str] = None
    due_date: Optional[date] = None


# ── Helpers ────────────────────────────────────────────────────────────


def _can_patch(principal: AuthPrincipal) -> bool:
    return (
        principal.is_tenant_admin
        or principal.source == "api_key"
        or bool(principal.manager_domains)
    )


async def _load_customer(
    db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID
) -> Customer:
    c = await db.get(Customer, customer_id)
    if c is None or c.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Customer not found")
    return c


def _concern_out(c: CustomerConcern) -> ConcernOut:
    return ConcernOut(
        id=c.id,
        topic=c.topic,
        description=c.description,
        status=c.status,
        severity=c.severity,
        source_motion=c.source_motion,
        first_seen_at=c.first_seen_at,
        last_seen_at=c.last_seen_at,
        resolved_at=c.resolved_at,
        status_changed_at=c.status_changed_at,
        evidence_count=len(c.evidence or []),
    )


def _commitment_out(cm: CustomerCommitment) -> CommitmentOut:
    return CommitmentOut(
        id=cm.id,
        description=cm.description,
        quote=cm.quote,
        due_date=cm.due_date,
        status=cm.status,
        met_at=cm.met_at,
        source_interaction_id=cm.source_interaction_id,
        created_at=cm.created_at,
    )


# ── Routes ─────────────────────────────────────────────────────────────


@router.get(
    "/customers/{customer_id}/memory",
    response_model=CustomerMemoryOut,
)
async def get_customer_memory(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> CustomerMemoryOut:
    """Full memory view: concerns + commitments + customer name.

    Concerns ordered by status (active first), then severity, then
    last_seen desc — keeps the hot stuff up top. Commitments newest-
    first.
    """
    c = await _load_customer(db, tenant.id, customer_id)
    concerns_rows = (
        await db.execute(
            select(CustomerConcern)
            .where(
                CustomerConcern.tenant_id == tenant.id,
                CustomerConcern.customer_id == customer_id,
            )
            .order_by(
                # Active first; map status to a sort key.
                CustomerConcern.last_seen_at.desc()
            )
        )
    ).scalars().all()
    # Stable status-aware sort done in Python — sqlalchemy ``case`` for
    # SQLite is verbose for small N.
    status_rank = {"active": 0, "monitoring": 1, "dormant": 2, "resolved": 3}
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    concerns_sorted = sorted(
        concerns_rows,
        key=lambda r: (
            status_rank.get(r.status, 9),
            severity_rank.get(r.severity, 9),
            -(r.last_seen_at.timestamp() if r.last_seen_at else 0),
        ),
    )
    commitments_rows = (
        await db.execute(
            select(CustomerCommitment)
            .where(
                CustomerCommitment.tenant_id == tenant.id,
                CustomerCommitment.customer_id == customer_id,
            )
            .order_by(CustomerCommitment.created_at.desc())
        )
    ).scalars().all()
    return CustomerMemoryOut(
        customer_id=c.id,
        customer_name=c.name,
        concerns=[_concern_out(r) for r in concerns_sorted],
        commitments=[_commitment_out(r) for r in commitments_rows],
    )


@router.patch(
    "/customers/{customer_id}/concerns/{concern_id}",
    response_model=ConcernOut,
)
async def patch_concern(
    customer_id: uuid.UUID,
    concern_id: uuid.UUID,
    body: ConcernPatchIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> ConcernOut:
    if not _can_patch(principal):
        raise HTTPException(
            status_code=403, detail="Requires manager scope or tenant admin"
        )
    row = await db.get(CustomerConcern, concern_id)
    if (
        row is None
        or row.tenant_id != tenant.id
        or row.customer_id != customer_id
    ):
        raise HTTPException(status_code=404, detail="Concern not found")
    updates = body.model_dump(exclude_none=True)
    now = datetime.now(timezone.utc)
    if "status" in updates and updates["status"] != row.status:
        row.status_changed_at = now
        if updates["status"] == "resolved":
            row.resolved_at = now
        else:
            row.resolved_at = None
    for k, v in updates.items():
        setattr(row, k, v)
    await db.commit()
    return _concern_out(row)


@router.patch(
    "/customers/{customer_id}/commitments/{commitment_id}",
    response_model=CommitmentOut,
)
async def patch_commitment(
    customer_id: uuid.UUID,
    commitment_id: uuid.UUID,
    body: CommitmentPatchIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> CommitmentOut:
    if not _can_patch(principal):
        raise HTTPException(
            status_code=403, detail="Requires manager scope or tenant admin"
        )
    row = await db.get(CustomerCommitment, commitment_id)
    if (
        row is None
        or row.tenant_id != tenant.id
        or row.customer_id != customer_id
    ):
        raise HTTPException(status_code=404, detail="Commitment not found")
    updates = body.model_dump(exclude_none=True)
    now = datetime.now(timezone.utc)
    if (
        "status" in updates
        and updates["status"] == "met"
        and row.status != "met"
    ):
        row.met_at = now
    for k, v in updates.items():
        setattr(row, k, v)
    await db.commit()
    return _commitment_out(row)
