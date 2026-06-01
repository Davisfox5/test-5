"""Minimal SCIM 2.0 endpoint for IDP push-provisioning.

Supports the operations Okta / Azure AD / Workspace actually issue
during a typical provisioning lifecycle:

* ``POST /scim/v2/Users`` — create or upsert (by ``externalId``). The
  ``groups`` claim in the body drives motion-scope resolution via
  ``MotionProvisioningRule``.
* ``GET /scim/v2/Users/{id}`` — single-user read.
* ``PATCH /scim/v2/Users/{id}`` — partial update (active flag,
  groups). Per RFC 7644 we accept the ``Operations`` envelope but
  only honor ``replace`` on the fields we care about.
* ``DELETE /scim/v2/Users/{id}`` — soft-delete (sets ``is_active=False``).

Auth: a per-tenant API key in the ``Authorization: Bearer`` header.
We reuse the existing ``require_scope("users:write")`` gate; the SPA
auth path doesn't fire SCIM, so there's no need for the Clerk JWT
machinery here.

Not yet implemented (deferred to v3 if anyone needs them):

* ``GET /Users`` (list/filter) — Okta only calls this for periodic
  reconciliation; the live push path is POST/PATCH which we cover.
* ``Schemas`` / ``ResourceTypes`` / ``ServiceProviderConfig`` —
  discovery endpoints, optional for most IDPs.
* SCIM Group resource — we map groups on the user, not as a
  first-class object.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    hash_password,
    require_scope,
)
from backend.app.db import get_db
from backend.app.models import ScimAccountLink, User
from backend.app.services.sso_provisioning import (
    apply_scopes_to_user,
    resolve_scopes_from_groups,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── SCIM payload shapes (subset we honor) ──────────────────────────────


class ScimName(BaseModel):
    givenName: Optional[str] = None
    familyName: Optional[str] = None


class ScimEmail(BaseModel):
    value: str
    primary: Optional[bool] = None
    type: Optional[str] = None


class ScimGroupRef(BaseModel):
    """SCIM Group reference. We use ``display`` (Okta) or ``value``
    (Azure AD) — whichever the IDP populated. Both map to the rule
    table's ``group_name``."""

    value: Optional[str] = None
    display: Optional[str] = None


class ScimUserIn(BaseModel):
    schemas: Optional[List[str]] = None
    externalId: str
    userName: str  # required by RFC; the IDP-side username (often email)
    name: Optional[ScimName] = None
    displayName: Optional[str] = None
    emails: Optional[List[ScimEmail]] = None
    active: Optional[bool] = True
    groups: Optional[List[ScimGroupRef]] = Field(default_factory=list)


class ScimUserOut(BaseModel):
    schemas: List[str] = ["urn:ietf:params:scim:schemas:core:2.0:User"]
    id: str  # local User UUID rendered as string
    externalId: str
    userName: str
    name: ScimName
    displayName: Optional[str]
    emails: List[ScimEmail]
    active: bool
    groups: List[ScimGroupRef]
    meta: Dict[str, Any]


class ScimPatchOp(BaseModel):
    op: str  # add | remove | replace
    path: Optional[str] = None
    value: Any = None


class ScimPatchIn(BaseModel):
    schemas: Optional[List[str]] = None
    Operations: List[ScimPatchOp]


# ── Helpers ────────────────────────────────────────────────────────────


def _flatten_groups(body_groups: Optional[List[ScimGroupRef]]) -> List[str]:
    out: List[str] = []
    for g in body_groups or []:
        name = g.display or g.value
        if isinstance(name, str) and name:
            out.append(name)
    return out


def _primary_email(body: ScimUserIn) -> str:
    for e in body.emails or []:
        if e.primary and e.value:
            return e.value.lower()
    if body.emails and body.emails[0].value:
        return body.emails[0].value.lower()
    if "@" in (body.userName or ""):
        return body.userName.lower()
    raise HTTPException(
        status_code=400,
        detail="No usable email address in the SCIM payload.",
    )


def _to_scim_out(user: User, link: ScimAccountLink) -> ScimUserOut:
    name_parts = (user.name or "").strip().split(" ", 1)
    given = name_parts[0] if name_parts else None
    family = name_parts[1] if len(name_parts) > 1 else None
    groups: List[ScimGroupRef] = []
    return ScimUserOut(
        id=str(user.id),
        externalId=link.external_id,
        userName=user.email,
        name=ScimName(givenName=given, familyName=family),
        displayName=user.name,
        emails=[ScimEmail(value=user.email, primary=True, type="work")],
        active=bool(user.is_active),
        groups=groups,
        meta={
            "resourceType": "User",
            "created": user.created_at.isoformat() if user.created_at else None,
            "lastModified": link.last_seen_at.isoformat()
            if link.last_seen_at
            else None,
            "location": f"/scim/v2/Users/{user.id}",
        },
    )


async def _load_link(
    db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> ScimAccountLink:
    link = (
        await db.execute(
            select(ScimAccountLink).where(
                ScimAccountLink.tenant_id == tenant_id,
                ScimAccountLink.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="SCIM link not found")
    return link


# ── POST /scim/v2/Users (create or upsert) ─────────────────────────────


@router.post(
    "/scim/v2/Users",
    response_model=ScimUserOut,
    status_code=201,
    dependencies=[Depends(require_scope("users:write"))],
)
async def scim_create_user(
    body: ScimUserIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_scope("users:write")),
) -> ScimUserOut:
    """Create a User + ScimAccountLink, or upsert when the
    ``externalId`` already maps to one. Per RFC 7644 §3.3 we should
    return 409 on duplicates; some IDPs (Okta) prefer idempotent
    upsert so they don't have to track delivery state. We pick the
    pragmatic path: if the link exists, return its current state with
    201 anyway. The IDP treats 201 as success and moves on.
    """
    tenant_id = principal.tenant.id
    email = _primary_email(body)
    group_names = _flatten_groups(body.groups)
    sync = getattr(db, "sync_session", None)
    if sync is None:
        raise HTTPException(
            status_code=500,
            detail="No sync session adapter on the request DB binding.",
        )
    resolved = resolve_scopes_from_groups(sync, tenant_id, group_names)

    existing_link = (
        await db.execute(
            select(ScimAccountLink).where(
                ScimAccountLink.tenant_id == tenant_id,
                ScimAccountLink.external_id == body.externalId,
            )
        )
    ).scalar_one_or_none()

    if existing_link is not None:
        user = await db.get(User, existing_link.user_id)
        if user is None:
            raise HTTPException(
                status_code=500, detail="SCIM link orphaned (user missing)."
            )
        # Update mutable fields. ``email`` is the user's login identity
        # — only change it when the IDP says so (an Okta rename).
        if user.email != email:
            user.email = email
        if body.displayName and user.name != body.displayName:
            user.name = body.displayName
        if body.active is not None and bool(body.active) != user.is_active:
            user.is_active = bool(body.active)
        apply_scopes_to_user(sync, user, resolved)
        existing_link.last_seen_at = datetime.now(timezone.utc)
        await db.commit()
        return _to_scim_out(user, existing_link)

    # New user. Email collision within the platform is an error per the
    # existing /users contract; SCIM treats that as 409.
    collision = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if collision is not None:
        raise HTTPException(
            status_code=409,
            detail=f"User with email {email!r} already exists.",
        )

    display = body.displayName or (
        f"{body.name.givenName or ''} {body.name.familyName or ''}".strip()
        if body.name
        else None
    )
    # SCIM-provisioned users get a random password they cannot use to
    # log in directly. They authenticate through SSO; the password
    # column is only here so the column isn't NULL on a row whose
    # auth path doesn't need it.
    random_pw = hash_password(uuid.uuid4().hex)
    user = User(
        tenant_id=tenant_id,
        email=email,
        name=display or None,
        role="agent",
        password_hash=random_pw,
        is_active=bool(body.active) if body.active is not None else True,
        agent_domains=resolved.agent_domains,
        manager_domains=resolved.manager_domains,
        is_tenant_admin=resolved.is_tenant_admin,
    )
    db.add(user)
    await db.flush()
    link = ScimAccountLink(
        tenant_id=tenant_id,
        user_id=user.id,
        external_id=body.externalId,
        provider="scim",
    )
    db.add(link)
    await db.commit()
    return _to_scim_out(user, link)


@router.get(
    "/scim/v2/Users/{user_id}",
    response_model=ScimUserOut,
    dependencies=[Depends(require_scope("users:write"))],
)
async def scim_get_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_scope("users:write")),
) -> ScimUserOut:
    user = await db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="User not found")
    link = await _load_link(db, principal.tenant.id, user_id)
    return _to_scim_out(user, link)


@router.patch(
    "/scim/v2/Users/{user_id}",
    response_model=ScimUserOut,
    dependencies=[Depends(require_scope("users:write"))],
)
async def scim_patch_user(
    user_id: uuid.UUID,
    body: ScimPatchIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_scope("users:write")),
) -> ScimUserOut:
    """RFC 7644 §3.5.2 patch. We honor:

    * ``replace`` on ``active`` (the IDP toggles a user off/on)
    * ``replace`` on ``groups`` (a membership change → re-resolve scopes)
    * everything else logs at info and continues — Okta sends a lot of
      ops for fields we don't model (locale, timezone) and rejecting
      them would flood the IDP's error queue.
    """
    user = await db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="User not found")
    link = await _load_link(db, principal.tenant.id, user_id)

    new_groups: Optional[List[str]] = None
    new_active: Optional[bool] = None
    for op_ in body.Operations:
        op_name = (op_.op or "").lower()
        path = (op_.path or "").lower()
        if op_name not in {"replace", "add"}:
            continue
        if path == "active":
            new_active = bool(op_.value)
        elif path == "groups":
            # ``value`` is a list of group refs ({display, value}) or
            # bare strings depending on the IDP. Normalize.
            raw = op_.value if isinstance(op_.value, list) else []
            names: List[str] = []
            for item in raw:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    name = item.get("display") or item.get("value")
                    if isinstance(name, str):
                        names.append(name)
            new_groups = names

    if new_active is not None:
        user.is_active = new_active
    if new_groups is not None:
        sync = getattr(db, "sync_session", None)
        if sync is None:
            raise HTTPException(
                status_code=500,
                detail="No sync session adapter on the request DB binding.",
            )
        resolved = resolve_scopes_from_groups(
            sync, principal.tenant.id, new_groups
        )
        apply_scopes_to_user(sync, user, resolved)
    link.last_seen_at = datetime.now(timezone.utc)
    await db.commit()
    return _to_scim_out(user, link)


@router.delete(
    "/scim/v2/Users/{user_id}",
    status_code=204,
    dependencies=[Depends(require_scope("users:write"))],
)
async def scim_delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_scope("users:write")),
) -> None:
    """Soft-delete: matches the existing ``/users`` contract. SCIM
    callers expect a 204; we never hard-delete because that would
    break FK references on action items / audit rows."""
    user = await db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = False
    await db.commit()
    return None
