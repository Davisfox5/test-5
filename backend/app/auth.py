"""Authentication utilities — API key hashing, tenant resolution, and auth dependencies."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.db import get_db
from backend.app.models import ApiKey, Tenant, User


# ── API Key Helpers ──────────────────────────────────────


def hash_api_key(key: str) -> str:
    """SHA-256 hash an API key for secure storage/lookup."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_api_key() -> Tuple[str, str]:
    """Generate a cryptographically random API key.

    Returns:
        (plaintext_key, hashed_key) — the plaintext is shown once to the user,
        the hash is stored in the database.
    """
    plaintext = "csk_" + secrets.token_urlsafe(48)
    hashed = hash_api_key(plaintext)
    return plaintext, hashed


# ── FastAPI Dependencies ─────────────────────────────────


def _extract_bearer_token(request: Request) -> Optional[str]:
    """Extract a Bearer token from the Authorization header."""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


async def get_current_tenant(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Resolve the current tenant from an API key in the Authorization header.

    - Reads ``Authorization: Bearer <api_key>``
    - SHA-256 hashes the key and looks up the ``api_keys`` table
    - Returns the associated :class:`Tenant`
    - Raises 401 if the key is missing, invalid, or expired
    - Updates ``last_used_at`` on the matched API key row
    """
    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing API key")

    key_hash = hash_api_key(token)
    stmt = (
        select(ApiKey)
        .options(selectinload(ApiKey.tenant))
        .where(ApiKey.key_hash == key_hash)
    )
    result = await db.execute(stmt)
    api_key: Optional[ApiKey] = result.scalar_one_or_none()

    if api_key is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Check expiration
    if api_key.expires_at is not None:
        now = datetime.now(timezone.utc)
        if api_key.expires_at < now:
            raise HTTPException(status_code=401, detail="API key expired")

    # Touch last_used_at
    api_key.last_used_at = datetime.now(timezone.utc)

    return api_key.tenant


async def _resolve_clerk_user(
    request: Request,
    db: AsyncSession,
) -> Optional[Tenant]:
    """Attempt to resolve a tenant via Clerk JWT.

    **Hard-disabled.** The previous implementation treated the bearer token
    verbatim as a Clerk user ID, meaning any caller sending
    ``Authorization: Bearer clerk_<any-user-id>`` became that user's tenant.
    Until we wire real JWKS-backed signature + `iss`/`aud` verification, we
    refuse to accept Clerk bearer tokens altogether; callers must use an API
    key. Flip ``CLERK_JWT_VERIFIED`` to True once the verification is in.
    """
    return None


async def get_current_user_or_tenant(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Try JWT (Clerk) first, fall back to API key.

    This allows both browser-based (Clerk session) and programmatic (API key)
    access to the same endpoints.
    """
    # 1. Try Clerk JWT
    tenant = await _resolve_clerk_user(request, db)
    if tenant is not None:
        return tenant

    # 2. Fall back to API key
    return await get_current_tenant(request, db)


# ── Admin gate ───────────────────────────────────────────
# Everything under /api/v1/admin/ uses this dependency instead of
# get_current_tenant. An API key qualifies as admin only if its `scopes`
# JSONB array contains the literal string "admin". The default scopes
# for a minted key are ["read:all", "write:all"] — admin must be granted
# explicitly by a tenant admin via the API-key management UI.


async def get_current_admin(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Resolve the current tenant and require admin scope on the API key."""
    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing API key")

    key_hash = hash_api_key(token)
    stmt = (
        select(ApiKey)
        .options(selectinload(ApiKey.tenant))
        .where(ApiKey.key_hash == key_hash)
    )
    result = await db.execute(stmt)
    api_key: Optional[ApiKey] = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if api_key.expires_at is not None:
        if api_key.expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="API key expired")

    scopes = api_key.scopes or []
    if "admin" not in scopes:
        raise HTTPException(status_code=403, detail="Admin scope required")

    api_key.last_used_at = datetime.now(timezone.utc)
    return api_key.tenant
