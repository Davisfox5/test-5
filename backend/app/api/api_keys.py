"""API Keys management endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import generate_api_key, get_current_tenant, hash_api_key
from backend.app.db import get_db
from backend.app.models import ApiKey, Tenant

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class ApiKeyCreateRequest(BaseModel):
    name: Optional[str] = Field(None, description="Human-friendly label for the key")
    expires_at: Optional[datetime] = Field(None, description="Expiration timestamp (UTC)")
    # NOTE: ``scopes`` is intentionally absent from the public schema.
    # The DB column still exists and defaults to ["read:all", "write:all"];
    # nothing in ``auth.py`` enforces those values yet, so exposing them
    # via the API would imply guarantees we don't make. Re-add this
    # field once scope enforcement lands (deferred — too big for this batch).


class ApiKeyCreateResponse(BaseModel):
    """Returned exactly once — the plaintext key is never stored or shown again."""
    id: uuid.UUID
    name: Optional[str]
    key: str = Field(..., description="Plaintext API key — save it now, it will not be shown again")
    expires_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyOut(BaseModel):
    """Public representation — never includes the key itself.

    Scopes were dropped from this shape because they were decorative —
    the DB stored values weren't checked anywhere. Re-add when scope
    enforcement is wired into ``auth.py``.
    """
    id: uuid.UUID
    name: Optional[str]
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Endpoints ────────────────────────────────────────────


@router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=201)
async def create_api_key(
    body: ApiKeyCreateRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Generate a new API key for the current tenant.

    The plaintext key is returned **once** in the response body.
    Only the SHA-256 hash is stored in the database.
    """
    plaintext, hashed = generate_api_key()

    api_key = ApiKey(
        tenant_id=tenant.id,
        key_hash=hashed,
        name=body.name,
        # Hard-code the legacy "all access" scopes on the row. We can't
        # drop the column without a migration, but since auth.py never
        # reads these values, every key gets full tenant access regardless.
        scopes=["read:all", "write:all"],
        expires_at=body.expires_at,
    )
    db.add(api_key)
    await db.flush()

    return ApiKeyCreateResponse(
        id=api_key.id,
        name=api_key.name,
        key=plaintext,
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


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Revoke an API key.

    Soft-delete via ``revoked_at`` so the audit row survives. Auth
    lookups exclude revoked rows so the key stops authenticating
    immediately. Hard-deleting was the previous behaviour and meant
    "this tenant rotated keys" was unrecoverable from logs alone.
    """
    stmt = select(ApiKey).where(
        ApiKey.id == key_id,
        ApiKey.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(timezone.utc)
