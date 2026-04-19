"""Authentication utilities.

Two independent credential types:

* **Tenant-wide API key** — programmatic access (resellers, integrations,
  scripts). Identifies the tenant, not a specific human. Resolves to a
  *synthetic* admin principal so tenant-admin operations still work for
  key-based callers.
* **Per-user session JWT** — human dashboard access. Identifies the user,
  the tenant they belong to, and their role. Gate admin pages off this.

The FastAPI deps:

* ``get_current_tenant`` — legacy; resolves the tenant from either credential.
  Keeps existing endpoints working untouched.
* ``get_current_principal`` — new; resolves ``AuthPrincipal(tenant, user,
  role, source)``. Endpoints that need to know *which human* is calling
  (audit, role gates) depend on this.
* ``require_role("admin")`` — factory that returns a dep asserting the
  current principal has at least that role. Order: admin > manager > agent.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Callable, Optional, Tuple

import bcrypt
from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import ApiKey, Tenant, User

logger = logging.getLogger(__name__)


ROLES = ("agent", "manager", "admin")
_ROLE_RANK = {"agent": 1, "manager": 2, "admin": 3}


# ── Credential helpers ─────────────────────────────────────────────────


def hash_api_key(key: str) -> str:
    """SHA-256 hash an API key for secure storage/lookup."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_api_key() -> Tuple[str, str]:
    """Generate a cryptographically random API key.

    Returns (plaintext_key, hashed_key) — plaintext is shown once to the
    user, the hash is stored in the database.
    """
    plaintext = "csk_" + secrets.token_urlsafe(48)
    hashed = hash_api_key(plaintext)
    return plaintext, hashed


# ── Password hashing (bcrypt, 12 rounds) ───────────────────────────────


def hash_password(plain: str) -> str:
    """Return a bcrypt hash for a password. Must not be called on empty
    strings; the caller should pre-validate."""
    if not plain:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode(
        "utf-8"
    )


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # Malformed hash — treat as non-match rather than 500.
        return False


# ── Session JWTs ───────────────────────────────────────────────────────


_JWT_ALGORITHM = "HS256"


@lru_cache(maxsize=1)
def _jwt_secret() -> str:
    settings = get_settings()
    secret = settings.SESSION_JWT_SECRET or ""
    if not secret:
        if settings.DEBUG:
            ephemeral = secrets.token_urlsafe(48)
            logger.warning(
                "SESSION_JWT_SECRET not set; generated ephemeral key for DEBUG. "
                "Issued sessions will not survive a process restart."
            )
            return ephemeral
        raise RuntimeError(
            "SESSION_JWT_SECRET must be set in production. Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return secret


def _jwt_secret_reset_for_tests() -> None:
    _jwt_secret.cache_clear()


def issue_session_token(user: User) -> str:
    """Return an encoded JWT for a logged-in user. Stateless — validated
    on the way in via ``_decode_session_token``."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "tid": str(user.tenant_id),
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.SESSION_JWT_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_JWT_ALGORITHM)


def _decode_session_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[_JWT_ALGORITHM])
    except JWTError:
        return None


# ── Principal ──────────────────────────────────────────────────────────


@dataclass
class AuthPrincipal:
    """The identity behind a request.

    ``user`` is None only for API-key-authenticated calls where no local
    user row has been associated (the normal case today). In that mode
    we still treat the caller as tenant-admin — API keys are programmatic
    tenant credentials, not end-user credentials.
    """

    tenant: Tenant
    user: Optional[User]
    role: str  # agent | manager | admin
    source: str  # "api_key" | "session" | "clerk"

    @property
    def user_id(self) -> Optional[uuid.UUID]:
        return self.user.id if self.user else None


# ── FastAPI dependencies ───────────────────────────────────────────────


def _extract_bearer_token(request: Request) -> Optional[str]:
    """Extract a Bearer token from the Authorization header."""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


async def _principal_from_session_jwt(
    request: Request, db: AsyncSession
) -> Optional[AuthPrincipal]:
    """If the Bearer token is a signed session JWT, resolve it to the user."""
    token = _extract_bearer_token(request)
    if not token or token.startswith("csk_") or token.startswith("clerk_"):
        return None
    payload = _decode_session_token(token)
    if payload is None:
        return None
    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except (TypeError, ValueError):
        return None

    stmt = (
        select(User)
        .options(selectinload(User.tenant))
        .where(User.id == user_id, User.is_active.is_(True))
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        return None
    return AuthPrincipal(
        tenant=user.tenant,
        user=user,
        role=user.role or "agent",
        source="session",
    )


async def _principal_from_api_key(
    request: Request, db: AsyncSession
) -> Optional[AuthPrincipal]:
    """If the Bearer token is a tenant API key, resolve to a synthetic
    admin principal (no User row attached)."""
    token = _extract_bearer_token(request)
    if not token:
        return None
    key_hash = hash_api_key(token)
    stmt = (
        select(ApiKey)
        .options(selectinload(ApiKey.tenant))
        .where(ApiKey.key_hash == key_hash)
    )
    api_key = (await db.execute(stmt)).scalar_one_or_none()
    if api_key is None:
        return None

    if api_key.expires_at is not None:
        now = datetime.now(timezone.utc)
        if api_key.expires_at < now:
            raise HTTPException(status_code=401, detail="API key expired")

    api_key.last_used_at = datetime.now(timezone.utc)
    return AuthPrincipal(
        tenant=api_key.tenant,
        user=None,
        role="admin",  # tenant-wide key → tenant-admin scope
        source="api_key",
    )


async def get_current_principal(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthPrincipal:
    """Resolve the caller's identity.

    Tries session JWT first (dashboard traffic), then API key (programmatic).
    Raises 401 if neither succeeds.
    """
    principal = await _principal_from_session_jwt(request, db)
    if principal is not None:
        return principal

    principal = await _principal_from_api_key(request, db)
    if principal is not None:
        return principal

    raise HTTPException(status_code=401, detail="Missing or invalid credentials")


async def get_current_tenant(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Legacy dep kept for endpoints that only need the tenant.

    Accepts both session JWT and API key. New code should depend on
    :func:`get_current_principal` so role gates + audit can fire.
    """
    principal = await get_current_principal(request, db)
    return principal.tenant


def require_role(minimum: str) -> Callable[..., AuthPrincipal]:
    """Return a dependency that asserts the current principal has at least
    the given role. Order: admin > manager > agent.

    ``require_role("admin")`` → only admins pass.
    ``require_role("manager")`` → managers + admins pass.
    """
    if minimum not in _ROLE_RANK:
        raise ValueError(f"unknown role: {minimum}")
    threshold = _ROLE_RANK[minimum]

    async def _dep(
        principal: AuthPrincipal = Depends(get_current_principal),
    ) -> AuthPrincipal:
        if _ROLE_RANK.get(principal.role, 0) < threshold:
            raise HTTPException(
                status_code=403,
                detail=f"Requires role >= {minimum}",
            )
        return principal

    _dep.__name__ = f"require_role_{minimum}"
    return _dep


# ── Transitional Clerk stub (unchanged behaviour) ──────────────────────


async def _resolve_clerk_user(request: Request, db: AsyncSession) -> Optional[Tenant]:
    """Legacy Clerk stub kept for older callers. New code should not use this."""
    token = _extract_bearer_token(request)
    if token is None or not token.startswith("clerk_"):
        return None
    clerk_user_id = token  # placeholder — replace with real JWT verification
    stmt = (
        select(User)
        .options(selectinload(User.tenant))
        .where(User.clerk_user_id == clerk_user_id)
    )
    result = await db.execute(stmt)
    user: Optional[User] = result.scalar_one_or_none()
    if user is None:
        return None
    return user.tenant


async def get_current_user_or_tenant(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Try Clerk JWT first, fall back to the unified principal resolver."""
    tenant = await _resolve_clerk_user(request, db)
    if tenant is not None:
        return tenant
    return await get_current_tenant(request, db)
