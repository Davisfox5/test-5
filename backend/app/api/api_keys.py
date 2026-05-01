"""API Keys management endpoints.

Endpoints surface the new scope namespace defined in
:mod:`backend.app.auth` (see :data:`backend.app.auth.API_KEY_SCOPES`):

* ``POST /api-keys`` — accepts ``scopes: list[str]``. Empty / omitted
  defaults to a small read-only canonical set
  (:data:`DEFAULT_READ_ONLY_SCOPES`); pass ``["*"]`` for the legacy
  "all access" opt-in. Unknown scopes 422.
* ``PATCH /api-keys/{id}`` — partial update of ``name`` / ``scopes`` /
  ``expires_at``. The plaintext key never changes (rotate by creating
  a new key + revoking the old one).
* ``DELETE /api-keys/{id}`` — soft-delete via ``revoked_at``.

Scope enforcement is applied across the rest of the API via
:func:`backend.app.auth.require_scope` — see the per-route map at
``docs/api_key_scope_map.yaml``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    API_KEY_SCOPES,
    AuthPrincipal,
    generate_api_key,
    get_current_principal,
    get_current_tenant,
    require_scope,
    validate_scopes,
)
from backend.app.db import get_db
from backend.app.models import ApiKey, Tenant
from backend.app.services.audit_log import audit_log

router = APIRouter()


# ── Defaults ─────────────────────────────────────────────

# A key created without an explicit ``scopes`` field gets these — a
# minimal "look at my data" set. Writes still 403 until an admin
# PATCHes the key with explicit write scopes (or ``["*"]``).
DEFAULT_READ_ONLY_SCOPES: list[str] = [
    "interactions:read",
    "action_items:read",
    "analytics:read",
    "contacts:read",
    "kb:read",
    "scorecards:read",
    "users:read",
    "webhooks:read",
]


# ── Pydantic Schemas ─────────────────────────────────────


class ApiKeyCreateRequest(BaseModel):
    name: Optional[str] = Field(None, description="Human-friendly label for the key")
    expires_at: Optional[datetime] = Field(None, description="Expiration timestamp (UTC)")
    scopes: Optional[List[str]] = Field(
        None,
        description=(
            "List of canonical scopes. Omit (or pass null) to default to a "
            "read-only set. Pass ``[\"*\"]`` for legacy all-access. Unknown "
            "scopes are rejected with 422."
        ),
    )

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        try:
            return validate_scopes(v)
        except ValueError as exc:
            # Pydantic surfaces ValueError as a 422 in the response.
            raise ValueError(str(exc))


class ApiKeyUpdateRequest(BaseModel):
    """Partial update — every field is optional."""

    name: Optional[str] = None
    expires_at: Optional[datetime] = None
    scopes: Optional[List[str]] = None

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        try:
            return validate_scopes(v)
        except ValueError as exc:
            raise ValueError(str(exc))


class ApiKeyCreateResponse(BaseModel):
    """Returned exactly once — the plaintext key is never stored or shown again."""
    id: uuid.UUID
    name: Optional[str]
    key: str = Field(..., description="Plaintext API key — save it now, it will not be shown again")
    scopes: List[str]
    expires_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyOut(BaseModel):
    """Public representation — never includes the key itself."""

    id: uuid.UUID
    name: Optional[str]
    scopes: List[str]
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]
    created_at: datetime
    revoked_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ScopeCatalogResponse(BaseModel):
    scopes: List[str]
    default_read_only: List[str]


# ── Endpoints ────────────────────────────────────────────


@router.get("/api-keys/scopes", response_model=ScopeCatalogResponse)
async def list_scope_catalog(
    tenant: Tenant = Depends(get_current_tenant),
) -> ScopeCatalogResponse:
    """Return the canonical scope namespace + the default read-only subset.

    Used by the SPA's API key editor to render the scope multi-select.
    Authenticated read — every tenant sees the same list.
    """
    return ScopeCatalogResponse(
        scopes=sorted(API_KEY_SCOPES),
        default_read_only=DEFAULT_READ_ONLY_SCOPES,
    )


@router.post(
    "/api-keys",
    response_model=ApiKeyCreateResponse,
    status_code=201,
    dependencies=[Depends(require_scope("api_keys:write"))],
)
async def create_api_key(
    body: ApiKeyCreateRequest,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Generate a new API key for the current tenant.

    The plaintext key is returned **once** in the response body.
    Only the SHA-256 hash is stored in the database. ``scopes`` defaults
    to the read-only canonical set; pass ``["*"]`` for full access.
    """
    plaintext, hashed = generate_api_key()
    scopes = body.scopes if body.scopes is not None else list(DEFAULT_READ_ONLY_SCOPES)

    api_key = ApiKey(
        tenant_id=principal.tenant.id,
        key_hash=hashed,
        name=body.name,
        scopes=scopes,
        expires_at=body.expires_at,
    )
    db.add(api_key)
    await db.flush()

    await audit_log(
        db,
        principal,
        action="api_key.created",
        resource_type="api_key",
        resource_id=str(api_key.id),
        after={"name": api_key.name, "scopes": scopes, "expires_at": str(api_key.expires_at) if api_key.expires_at else None},
    )

    return ApiKeyCreateResponse(
        id=api_key.id,
        name=api_key.name,
        key=plaintext,
        scopes=scopes,
        expires_at=api_key.expires_at,
        created_at=api_key.created_at,
    )


@router.get("/api-keys", response_model=List[ApiKeyOut])
async def list_api_keys(
    include_revoked: bool = False,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """List all API keys for the current tenant.

    Returns metadata only — the key hash/plaintext is never exposed. By
    default revoked keys are filtered out; pass ``include_revoked=true``
    for an audit-trail view of every key that ever existed.
    """
    stmt = select(ApiKey).where(ApiKey.tenant_id == tenant.id)
    if not include_revoked:
        stmt = stmt.where(ApiKey.revoked_at.is_(None))
    stmt = stmt.order_by(ApiKey.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.patch(
    "/api-keys/{key_id}",
    response_model=ApiKeyOut,
    dependencies=[Depends(require_scope("api_keys:write"))],
)
async def update_api_key(
    key_id: uuid.UUID,
    body: ApiKeyUpdateRequest,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Partial update of name / scopes / expires_at.

    The plaintext key never changes. To rotate the secret material,
    create a new key and revoke the old one.
    """
    stmt = select(ApiKey).where(
        ApiKey.id == key_id,
        ApiKey.tenant_id == principal.tenant.id,
        ApiKey.revoked_at.is_(None),
    )
    result = await db.execute(stmt)
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    before = {
        "name": api_key.name,
        "scopes": list(api_key.scopes or []),
        "expires_at": str(api_key.expires_at) if api_key.expires_at else None,
    }

    # Use Pydantic's exclude_unset semantics so partial updates don't
    # clobber unspecified fields with None.
    payload = body.model_dump(exclude_unset=True)
    if "name" in payload:
        api_key.name = payload["name"]
    if "expires_at" in payload:
        api_key.expires_at = payload["expires_at"]
    if "scopes" in payload:
        api_key.scopes = payload["scopes"] or []

    await db.flush()

    after = {
        "name": api_key.name,
        "scopes": list(api_key.scopes or []),
        "expires_at": str(api_key.expires_at) if api_key.expires_at else None,
    }
    await audit_log(
        db,
        principal,
        action="api_key.updated",
        resource_type="api_key",
        resource_id=str(api_key.id),
        before=before,
        after=after,
    )

    return api_key


@router.delete(
    "/api-keys/{key_id}",
    status_code=204,
    dependencies=[Depends(require_scope("api_keys:write"))],
)
async def revoke_api_key(
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Revoke an API key.

    Soft-delete via ``revoked_at`` so the audit row survives. Auth
    lookups exclude revoked rows so the key stops authenticating
    immediately.
    """
    stmt = select(ApiKey).where(
        ApiKey.id == key_id,
        ApiKey.tenant_id == principal.tenant.id,
    )
    result = await db.execute(stmt)
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(timezone.utc)
        await db.flush()
        await audit_log(
            db,
            principal,
            action="api_key.revoked",
            resource_type="api_key",
            resource_id=str(api_key.id),
            before={"name": api_key.name, "scopes": list(api_key.scopes or [])},
        )
