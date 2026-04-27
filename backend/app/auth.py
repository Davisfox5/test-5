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

import base64
import hashlib
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Callable, Optional, Tuple

import bcrypt
import httpx
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


# ── Clerk JWT verification ────────────────────────────────────────────────

# Module-level JWKS cache. Clerk rotates keys infrequently; 1h TTL is a
# safe default. The dict mutation is fine in a single-process worker;
# in a multi-worker setup each process refreshes independently.
_CLERK_JWKS_CACHE: dict = {"data": None, "expires_at": 0.0}
_CLERK_JWKS_TTL_S = 3600.0


def _clerk_jwks_url() -> Optional[str]:
    """Derive the Clerk JWKS URL from CLERK_PUBLISHABLE_KEY.

    The publishable key embeds the frontend API host:
        pk_<test|live>_<base64url(host + "$")>
    Decoding gives e.g. ``glad-cicada-1.clerk.accounts.dev``; JWKS lives
    at ``<host>/.well-known/jwks.json``.
    """
    settings = get_settings()
    pub = (settings.CLERK_PUBLISHABLE_KEY or "").strip()
    parts = pub.split("_", 2)
    if len(parts) != 3 or parts[0] != "pk":
        return None
    payload = parts[2]
    # Add padding if missing — base64.urlsafe_b64decode wants `=` to align to 4.
    padded = payload + "=" * ((4 - len(payload) % 4) % 4)
    try:
        host = base64.urlsafe_b64decode(padded).decode("ascii").rstrip("$")
    except (ValueError, UnicodeDecodeError):
        return None
    if not host:
        return None
    return f"https://{host}/.well-known/jwks.json"


async def _fetch_clerk_jwks() -> Optional[dict]:
    """Fetch (and cache for an hour) Clerk's JWKS so we can verify session JWTs."""
    now = time.time()
    if _CLERK_JWKS_CACHE["data"] is not None and _CLERK_JWKS_CACHE["expires_at"] > now:
        return _CLERK_JWKS_CACHE["data"]
    url = _clerk_jwks_url()
    if url is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("clerk jwks fetch failed: %s", exc)
        return None
    _CLERK_JWKS_CACHE["data"] = data
    _CLERK_JWKS_CACHE["expires_at"] = now + _CLERK_JWKS_TTL_S
    return data


async def _principal_from_clerk(
    request: Request, db: AsyncSession
) -> Optional[AuthPrincipal]:
    """If the Bearer is a Clerk session JWT (prefixed ``clerk_`` by the SPA),
    verify it against Clerk's JWKS and resolve the User by ``clerk_user_id``.

    The SPA wraps the Clerk session token as ``Bearer clerk_<JWT>``. We
    strip the prefix, validate the JWT signature against Clerk's public
    keys, and read ``sub`` (the Clerk user id, e.g. ``user_2lj…``) to
    look up the matching ``users`` row written at /trial/signup time.
    """
    token = _extract_bearer_token(request)
    if not token or not token.startswith("clerk_"):
        return None
    jwt_token = token[len("clerk_"):]
    if not jwt_token:
        return None

    jwks = await _fetch_clerk_jwks()
    if jwks is None:
        return None

    try:
        unverified = jwt.get_unverified_header(jwt_token)
    except JWTError:
        return None
    kid = unverified.get("kid")
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key is None:
        return None

    try:
        # Clerk session JWTs use RS256. We don't constrain audience here —
        # it varies between dev/prod instances and clerk-frontend-api
        # (sub is what actually identifies the principal).
        payload = jwt.decode(
            jwt_token,
            key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
    except JWTError:
        return None

    clerk_user_id = payload.get("sub")
    if not clerk_user_id:
        return None

    stmt = (
        select(User)
        .options(selectinload(User.tenant))
        .where(User.clerk_user_id == clerk_user_id, User.is_active.is_(True))
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        return None
    return AuthPrincipal(
        tenant=user.tenant,
        user=user,
        role=user.role or "agent",
        source="clerk",
    )


async def get_current_principal(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthPrincipal:
    """Resolve the caller's identity.

    Tries session JWT first (legacy dashboard traffic), then tenant API
    key (programmatic), then a Clerk session JWT (the Next.js SPA).
    Raises 401 if none succeed.

    As a side effect, updates the ``tenant_id`` / ``user_id`` context
    vars so every log line the request fans out to carries them.
    """
    from backend.app.logging_setup import bind_context

    principal = await _principal_from_session_jwt(request, db)
    if principal is None:
        principal = await _principal_from_api_key(request, db)
    if principal is None:
        principal = await _principal_from_clerk(request, db)
    if principal is None:
        raise HTTPException(status_code=401, detail="Missing or invalid credentials")

    bind_context(
        tenant_id=str(principal.tenant.id),
        user_id=str(principal.user_id) if principal.user_id else None,
    )
    return principal


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


# Clerk stub removed. Native session JWTs + per-tenant API keys cover
# every auth path we need. ``User.clerk_user_id`` is still on the model
# for future Clerk integration, but nothing consumes it today.
