"""SSO/SCIM admin API.

Two surfaces:

* ``/admin/motion-provisioning-rules`` — tenant-admin CRUD over the
  IDP-group → motion-scope mapping. Drives the Settings UI.
* ``/admin/sso/test-resolve`` — dry-run helper. Given a list of group
  names, returns the resolved scopes without touching any user.
  Useful when an admin is setting up rules and wants to confirm a
  user with those groups would land where they expect.

The SCIM ``POST /scim/v2/Users`` endpoint lives in ``api/scim.py``;
the rule CRUD here is the dependency that endpoint relies on.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    CANONICAL_DOMAINS,
    require_role,
)
from backend.app.db import get_db
from backend.app.models import MotionProvisioningRule

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic shapes ────────────────────────────────────────────────────


class MotionRuleOut(BaseModel):
    id: uuid.UUID
    group_name: str
    agent_domains: List[str]
    manager_domains: List[str]
    grants_tenant_admin: bool
    is_active: bool
    description: Optional[str]
    created_at: datetime
    updated_at: datetime


class MotionRuleIn(BaseModel):
    group_name: str = Field(..., min_length=1, max_length=255)
    agent_domains: List[str] = Field(default_factory=list)
    manager_domains: List[str] = Field(default_factory=list)
    grants_tenant_admin: bool = False
    is_active: bool = True
    description: Optional[str] = None


class MotionRulePatchIn(BaseModel):
    group_name: Optional[str] = None
    agent_domains: Optional[List[str]] = None
    manager_domains: Optional[List[str]] = None
    grants_tenant_admin: Optional[bool] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None


class TestResolveIn(BaseModel):
    group_names: List[str]


class TestResolveOut(BaseModel):
    matched_rule_count: int
    agent_domains: List[str]
    manager_domains: List[str]
    is_tenant_admin: bool


# ── Validators ─────────────────────────────────────────────────────────


def _validate_domains(values: List[str], field: str) -> List[str]:
    out: List[str] = []
    for v in values:
        if v not in CANONICAL_DOMAINS:
            raise HTTPException(
                status_code=422,
                detail=f"{field}: {v!r} is not a known domain",
            )
        if v not in out:
            out.append(v)
    return out


def _to_out(rule: MotionProvisioningRule) -> MotionRuleOut:
    return MotionRuleOut(
        id=rule.id,
        group_name=rule.group_name,
        agent_domains=list(rule.agent_domains or []),
        manager_domains=list(rule.manager_domains or []),
        grants_tenant_admin=rule.grants_tenant_admin,
        is_active=rule.is_active,
        description=rule.description,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


# ── Routes ─────────────────────────────────────────────────────────────


@router.get(
    "/admin/motion-provisioning-rules",
    response_model=List[MotionRuleOut],
    dependencies=[Depends(require_role("admin"))],
)
async def list_rules(
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
) -> List[MotionRuleOut]:
    rows = (
        await db.execute(
            select(MotionProvisioningRule)
            .where(MotionProvisioningRule.tenant_id == principal.tenant.id)
            .order_by(MotionProvisioningRule.group_name.asc())
        )
    ).scalars().all()
    return [_to_out(r) for r in rows]


@router.post(
    "/admin/motion-provisioning-rules",
    response_model=MotionRuleOut,
    status_code=201,
    dependencies=[Depends(require_role("admin"))],
)
async def create_rule(
    body: MotionRuleIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
) -> MotionRuleOut:
    agent_domains = _validate_domains(body.agent_domains, "agent_domains")
    manager_domains = _validate_domains(body.manager_domains, "manager_domains")
    # Reject duplicate (tenant, group) at the application layer so we
    # surface a 409 rather than letting the unique-constraint hit
    # surface a 500.
    existing = (
        await db.execute(
            select(MotionProvisioningRule).where(
                MotionProvisioningRule.tenant_id == principal.tenant.id,
                MotionProvisioningRule.group_name == body.group_name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Rule for group {body.group_name!r} already exists.",
        )
    rule = MotionProvisioningRule(
        tenant_id=principal.tenant.id,
        group_name=body.group_name,
        agent_domains=agent_domains,
        manager_domains=manager_domains,
        grants_tenant_admin=body.grants_tenant_admin,
        is_active=body.is_active,
        description=body.description,
    )
    db.add(rule)
    await db.flush()
    await db.commit()
    return _to_out(rule)


@router.patch(
    "/admin/motion-provisioning-rules/{rule_id}",
    response_model=MotionRuleOut,
    dependencies=[Depends(require_role("admin"))],
)
async def patch_rule(
    rule_id: uuid.UUID,
    body: MotionRulePatchIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
) -> MotionRuleOut:
    rule = await db.get(MotionProvisioningRule, rule_id)
    if rule is None or rule.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    updates = body.model_dump(exclude_none=True)
    if "agent_domains" in updates:
        updates["agent_domains"] = _validate_domains(
            updates["agent_domains"], "agent_domains"
        )
    if "manager_domains" in updates:
        updates["manager_domains"] = _validate_domains(
            updates["manager_domains"], "manager_domains"
        )
    for k, v in updates.items():
        setattr(rule, k, v)
    rule.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return _to_out(rule)


@router.delete(
    "/admin/motion-provisioning-rules/{rule_id}",
    status_code=204,
    dependencies=[Depends(require_role("admin"))],
)
async def delete_rule(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
) -> None:
    rule = await db.get(MotionProvisioningRule, rule_id)
    if rule is None or rule.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.delete(rule)
    await db.commit()
    return None


@router.post(
    "/admin/sso/test-resolve",
    response_model=TestResolveOut,
    dependencies=[Depends(require_role("admin"))],
)
async def test_resolve(
    body: TestResolveIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
) -> TestResolveOut:
    """Dry-run: which scopes would a user with these groups end up with?

    Doesn't touch any user or audit log. Pure SELECT against the rule
    table. The Settings UI uses this to validate a rule set without
    forcing the admin to log out and back in through SSO.
    """
    from backend.app.services.sso_provisioning import resolve_scopes_from_groups

    # The async session adapter gives us a sync session for the resolver.
    sync = getattr(db, "sync_session", None)
    if sync is None:
        raise HTTPException(
            status_code=500,
            detail="No sync session adapter on the request DB binding.",
        )
    resolved = resolve_scopes_from_groups(sync, principal.tenant.id, body.group_names)
    return TestResolveOut(
        matched_rule_count=resolved.matched_rule_count,
        agent_domains=resolved.agent_domains,
        manager_domains=resolved.manager_domains,
        is_tenant_admin=resolved.is_tenant_admin,
    )
