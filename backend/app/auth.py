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
  role, source, scopes)``. Endpoints that need to know *which human* is
  calling (audit, role gates) depend on this.
* ``require_role("admin")`` — factory that returns a dep asserting the
  current principal has at least that role. Order: admin > manager > agent.
* ``require_scope("foo:bar")`` — factory that returns a dep asserting the
  current principal carries the named scope. Only enforced when
  ``source == "api_key"``; session-JWT and Clerk-JWT principals bypass
  the check (they're already gated by ``require_role``).

Canonical API-key scopes (see :data:`API_KEY_SCOPES`):

* ``interactions:read`` / ``interactions:write`` — call records, transcripts.
* ``action_items:read`` / ``action_items:write`` — follow-up items.
* ``analytics:read`` — dashboards, metrics, exports.
* ``webhooks:read`` / ``webhooks:write`` — outbound webhook config.
* ``kb:read`` / ``kb:write`` — knowledge-base docs and pins.
* ``crm:sync`` — trigger CRM sync runs.
* ``gdpr:export`` / ``gdpr:delete`` — data-subject endpoints.
* ``contacts:read`` / ``contacts:write`` — contacts + customers.
* ``scorecards:read`` / ``scorecards:write`` — scorecard templates.
* ``users:read`` / ``users:write`` — directory + role changes.
* ``api_keys:write`` — create / revoke API keys (rare — tenants usually
  do this from the dashboard).
* ``settings:write`` — tenant settings + feature flags.
* ``onboarding:write`` — onboarding session lifecycle.
* ``corrections:write`` / ``feedback:write`` — model-improvement signals.
* ``campaigns:write`` / ``experiments:write`` / ``evaluation:write`` —
  research surfaces.
* ``library:write`` — call library snippet promotion.
* ``oauth:write`` — start/revoke 3rd-party OAuth grants.
* ``audit_log:read`` — read the new admin audit log.
* ``*`` — every scope (the legacy "all access" opt-in).

A blank scope list (``[]``) grants no write access at all — read-only
GET endpoints don't require a scope, but every POST/PATCH/PUT/DELETE
will 403. This is intentional: keys created without explicit scopes
should fail closed.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
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


# Canonical scope namespace for API keys. Anything outside this set is
# rejected on key creation/update with HTTP 422. ``"*"`` is the wildcard
# meaning "every scope" — it's preserved as the explicit opt-in for the
# legacy "all-access" semantics. Add scopes here as new write surfaces
# land; keep this in sync with the per-route scope map at
# ``docs/api_key_scope_map.yaml``.
API_KEY_SCOPES: frozenset[str] = frozenset(
    {
        # data
        "interactions:read",
        "interactions:write",
        "action_items:read",
        "action_items:write",
        "notifications:read",
        "notifications:write",
        "contacts:read",
        "contacts:write",
        "analytics:read",
        "library:write",
        # config
        "webhooks:read",
        "webhooks:write",
        "kb:read",
        "kb:write",
        "scorecards:read",
        "scorecards:write",
        "settings:write",
        "users:read",
        "users:write",
        "api_keys:write",
        # integrations
        "crm:sync",
        "oauth:write",
        # GDPR
        "gdpr:export",
        "gdpr:delete",
        # workflow
        "onboarding:write",
        "corrections:write",
        "feedback:write",
        "campaigns:write",
        "experiments:write",
        "evaluation:write",
        # admin / observability
        "audit_log:read",
        # wildcard
        "*",
    }
)


def validate_scopes(scopes: list[str]) -> list[str]:
    """Validate + normalize a scope list.

    Strips whitespace, dedupes (preserving order), and rejects unknown
    values with ``ValueError``. ``"*"`` collapses to a single-element
    list (it subsumes everything else).
    """
    if not isinstance(scopes, list):
        raise ValueError("scopes must be a list of strings")
    seen: list[str] = []
    for raw in scopes:
        if not isinstance(raw, str):
            raise ValueError("scopes must be strings")
        s = raw.strip()
        if not s:
            continue
        if s not in API_KEY_SCOPES:
            raise ValueError(f"unknown scope: {s}")
        if s not in seen:
            seen.append(s)
    if "*" in seen:
        return ["*"]
    return seen


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

    ``scopes`` is only meaningful when ``source == "api_key"``; the
    ``require_scope`` dependency uses it to gate write endpoints.
    Session and Clerk principals get a ``["*"]`` placeholder so the
    dependency is a no-op for human callers (their gates are role-based
    via ``require_role``).

    ``is_previewing`` is True iff the sandbox role-preview override is
    being applied — i.e. ``role`` was overridden from
    ``user.preview_role`` rather than coming straight from
    ``user.role``. The SPA reads this to render the "preview mode"
    banner. Preview is render-time only; the underlying ``users.role``
    row is never mutated by the override path.
    """

    tenant: Tenant
    user: Optional[User]
    role: str  # agent | manager | admin
    source: str  # "api_key" | "session" | "clerk"
    scopes: list[str] = field(default_factory=lambda: ["*"])
    is_previewing: bool = False

    @property
    def user_id(self) -> Optional[uuid.UUID]:
        return self.user.id if self.user else None

    @property
    def real_role(self) -> str:
        """The user's actual ``users.role`` value (no preview overlay).

        Falls back to ``"agent"`` when no user is attached (e.g. an
        API-key principal) or when the column is NULL (legacy rows).
        Useful for the SPA to render "Switch back to admin" when the
        preview role differs from the real one.
        """
        if self.user is None:
            return self.role
        return self.user.role or "agent"

    def has_scope(self, scope: str) -> bool:
        """Return True if this principal carries the named scope.

        Session/Clerk principals are unaffected (their default ``["*"]``
        scopes always satisfy the check). API-key principals match
        against their granted scopes; ``"*"`` always wins.
        """
        if "*" in self.scopes:
            return True
        return scope in self.scopes


# ── Sandbox preview-role overlay ──────────────────────────────────────


_PREVIEW_ROLE_VALUES = frozenset({"agent", "manager", "admin"})


def _now_aware_for(dt: datetime) -> datetime:
    """Return ``datetime.now()`` matched to ``dt``'s tz-awareness.

    Postgres returns ``trial_ends_at`` as a tz-aware UTC datetime, but
    SQLite (used in tests) returns it naive. Matching the comparator
    avoids the ``can't compare offset-naive and offset-aware datetimes``
    TypeError without relaxing the production code path.
    """
    if dt.tzinfo is None:
        return datetime.utcnow()
    return datetime.now(timezone.utc)


def _resolve_effective_role(user: User, tenant: Tenant) -> Tuple[str, bool]:
    """Return ``(effective_role, is_previewing)`` for an interactive user.

    The sandbox preview overlay applies only when *all three* gates
    pass:

    1. The tenant is on the sandbox tier (the only free trial tier).
    2. The trial is still active (``trial_ends_at > now()``).
    3. ``user.preview_role`` is one of the three valid role names.

    On any failure the user's real ``users.role`` is returned (with a
    safe ``"agent"`` fallback for legacy NULL rows). The DB row is never
    mutated — preview is a render-time overlay, never a security
    boundary.
    """
    real = user.role or "agent"
    # ``getattr`` rather than direct access so legacy / mocked User and
    # Tenant objects (e.g. in tests, or hypothetical pre-migration
    # rows) don't explode here. The defaults all fall through to "no
    # preview" exactly as the production NULL / non-sandbox state would.
    preview = getattr(user, "preview_role", None)
    if preview not in _PREVIEW_ROLE_VALUES:
        return real, False
    if getattr(tenant, "plan_tier", None) != "sandbox":
        return real, False
    trial_ends_at = getattr(tenant, "trial_ends_at", None)
    if trial_ends_at is None:
        return real, False
    if trial_ends_at <= _now_aware_for(trial_ends_at):
        return real, False
    return preview, True


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
    """If the Bearer token is a signed session JWT, resolve it to the user.

    Skips API keys (csk_ prefix). Clerk JWTs aren't filtered here
    — they're RS256, so _decode_session_token (HS256 + our secret)
    will fail and we'll fall through to _principal_from_clerk.
    """
    token = _extract_bearer_token(request)
    if not token or token.startswith("csk_"):
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
    effective_role, is_previewing = _resolve_effective_role(user, user.tenant)
    # Warm the per-tenant cache so subsequent non-auth Tenant lookups (Celery
    # tasks, websocket handlers, services) skip the DB.
    try:
        from backend.app.services.tenant_cache import cache_set

        cache_set(user.tenant)
    except Exception:  # pragma: no cover — cache is best-effort
        pass
    return AuthPrincipal(
        tenant=user.tenant,
        user=user,
        role=effective_role,
        source="session",
        is_previewing=is_previewing,
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
        .where(
            ApiKey.key_hash == key_hash,
            # Soft-deleted keys keep their row (audit trail) but must
            # not authenticate. See backend/app/api/api_keys.py:revoke_api_key.
            ApiKey.revoked_at.is_(None),
        )
    )
    api_key = (await db.execute(stmt)).scalar_one_or_none()
    if api_key is None:
        return None

    if api_key.expires_at is not None:
        now = datetime.now(timezone.utc)
        if api_key.expires_at < now:
            raise HTTPException(status_code=401, detail="API key expired")

    api_key.last_used_at = datetime.now(timezone.utc)
    # Defensive: ``scopes`` defaults to ``[]`` at the column level, but
    # legacy rows from before the q4e5f6a7b8c9 migration may have NULL.
    raw_scopes = api_key.scopes or []
    try:
        from backend.app.services.tenant_cache import cache_set

        cache_set(api_key.tenant)
    except Exception:  # pragma: no cover — cache is best-effort
        pass
    return AuthPrincipal(
        tenant=api_key.tenant,
        user=None,
        role="admin",  # tenant-wide key → tenant-admin scope
        source="api_key",
        scopes=list(raw_scopes),
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
    """If the Bearer is a Clerk session JWT, verify it against Clerk's
    JWKS and resolve the User by ``clerk_user_id`` (the ``sub`` claim,
    e.g. ``user_2lj…``).

    Accepts either ``Bearer <JWT>`` (current SPA convention) or
    ``Bearer clerk_<JWT>`` (legacy — the prefix tripped Clerk's own
    Next.js middleware on the SPA, which tried to base64-decode the
    full string and threw ``Unexpected token 'r'... is not valid JSON``
    on every authenticated request).
    """
    token = _extract_bearer_token(request)
    if not token:
        return None
    if token.startswith("csk_"):
        return None
    jwt_token = token[len("clerk_"):] if token.startswith("clerk_") else token
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
    effective_role, is_previewing = _resolve_effective_role(user, user.tenant)
    return AuthPrincipal(
        tenant=user.tenant,
        user=user,
        role=effective_role,
        source="clerk",
        is_previewing=is_previewing,
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


def require_scope(scope: str) -> Callable[..., AuthPrincipal]:
    """Return a dependency that asserts the API-key principal has ``scope``.

    Behaviour:

    * ``source == "api_key"``: scope must be in ``principal.scopes`` or
      ``"*"`` must be present, else 403 ``{"detail": "missing scope: …"}``.
    * ``source == "session"`` or ``"clerk"``: bypass the check. Human
      callers are gated by ``require_role``; mixing scope checks in
      would force admins to also grant themselves API-key scopes, which
      is never the intent.

    Usage on a route::

        @router.post("/webhooks", dependencies=[Depends(require_scope("webhooks:write"))])
        async def create_webhook(...): ...

    The unknown-scope guard is intentional — typos in the source code
    fail loudly at import-time rather than silently letting every key
    through.
    """
    if scope not in API_KEY_SCOPES:
        raise ValueError(f"require_scope: unknown scope {scope!r}")

    async def _dep(
        principal: AuthPrincipal = Depends(get_current_principal),
    ) -> AuthPrincipal:
        if principal.source != "api_key":
            return principal
        if principal.has_scope(scope):
            return principal
        raise HTTPException(
            status_code=403,
            detail=f"missing scope: {scope}",
        )

    _dep.__name__ = f"require_scope_{scope.replace(':', '_')}"
    return _dep


# Clerk stub removed. Native session JWTs + per-tenant API keys cover
# every auth path we need. ``User.clerk_user_id`` is still on the model
# for future Clerk integration, but nothing consumes it today.
