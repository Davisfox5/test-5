"""Just-in-time user provisioning for Clerk-brokered enterprise SSO.

The Clerk broker validates a SAML/OIDC login (Okta / Microsoft Entra /
Google Workspace enterprise connection) and hands the SPA a Clerk session
JWT. ``auth._principal_from_clerk`` verifies that JWT but, until now,
resolved the principal **only** by matching an existing local ``User``
row on ``clerk_user_id`` — so a brand-new SSO user authenticated at Clerk
yet was rejected at LINDA because no row existed. That is the concrete
gap between "we advertise SSO" and "SSO works".

This module closes it. On a cache-miss login it:

1. Resolves the target ``Tenant`` from the token — first by a Clerk
   **organization id** claim (``org_id``), then by the **email domain** —
   using the per-tenant mapping stored in
   ``tenants.features_enabled['sso']``::

       {"sso": {
           "clerk_org_ids": ["org_2ab…"],   # authoritative mapping
           "email_domains": ["acme.com"],     # operator-verified fallback
           "jit_create": true,                 # allow creating net-new users
           "default_role": "agent",
           "default_agent_domains": ["customer_service"]
       }}

2. **Links** an existing invited / SCIM-provisioned user (same tenant,
   same email, no ``clerk_user_id`` yet) to this Clerk identity — the
   safe common case, no row created. Never hijacks an email already bound
   to a *different* Clerk id.

3. Otherwise **creates** a new user, but only when the tenant's SSO
   config sets ``jit_create``.

Writes to ``users`` are RLS-gated, so we arm the resolved tenant before
touching the row. Reads of ``tenants`` need no binding — it's a global
table. The whole path is gated by ``SSO_JIT_PROVISIONING_ENABLED`` and is
best-effort: any failure returns ``False`` and the caller falls back to
the pre-existing "reject unknown user" behaviour.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import Tenant, User
from backend.app.tenant_ctx import bind_tenant_async

logger = logging.getLogger(__name__)

_ALLOWED_ROLES = {"agent", "manager", "admin"}


async def resolve_or_provision_clerk_user(
    db: AsyncSession,
    payload: Dict[str, Any],
    clerk_user_id: str,
) -> bool:
    """Link or create a local User for a net-new Clerk SSO identity.

    Returns ``True`` when, after this call, a ``User`` row exists for
    ``clerk_user_id`` on a resolved tenant (the caller then re-loads it
    through the normal happy path). Returns ``False`` when JIT is disabled,
    no tenant maps to the token, or provisioning is not permitted — the
    caller treats that exactly like the old "unknown user" rejection.
    """
    if not get_settings().SSO_JIT_PROVISIONING_ENABLED:
        return False

    email = _extract_email(payload)
    org_id = _extract_org_id(payload)
    if not org_id and not email:
        return False

    tenant = await _resolve_tenant(db, org_id=org_id, email=email)
    if tenant is None:
        logger.info(
            "SSO JIT: no tenant mapped to clerk user %s (org_id=%s, email=%s) "
            "— rejecting per fail-closed default",
            clerk_user_id,
            org_id,
            _mask_email(email),
        )
        return False

    sso_cfg: Dict[str, Any] = dict((tenant.features_enabled or {}).get("sso") or {})

    # Writes to ``users`` require the tenant GUC armed (RLS WITH CHECK).
    await bind_tenant_async(db, tenant.id)

    # 1) Link an already-provisioned user by email (no row created).
    if email:
        existing = (
            await db.execute(
                select(User).where(
                    User.tenant_id == tenant.id,
                    func.lower(User.email) == email.lower(),
                    User.is_active.is_(True),
                )
            )
        ).scalars().first()
        if existing is not None:
            if existing.clerk_user_id is None:
                existing.clerk_user_id = clerk_user_id
                await db.commit()
                logger.info(
                    "SSO JIT: linked existing user %s to clerk id %s (tenant=%s)",
                    existing.id,
                    clerk_user_id,
                    tenant.id,
                )
                return True
            if existing.clerk_user_id == clerk_user_id:
                return True
            # Email already bound to a different Clerk identity — never
            # silently re-point it; a human must reconcile.
            logger.warning(
                "SSO JIT: email %s already bound to a different clerk id on "
                "tenant %s — refusing to re-link",
                _mask_email(email),
                tenant.id,
            )
            return False

    # 2) Create a net-new user only when the tenant opts in.
    if not sso_cfg.get("jit_create"):
        logger.info(
            "SSO JIT: tenant %s has no jit_create and no invited user for %s "
            "— rejecting",
            tenant.id,
            _mask_email(email),
        )
        return False
    if not email:
        # Can't create a user without an email (the column is NOT NULL and
        # it's the human-facing identity). Clerk needs an email claim in
        # the JWT template for create-mode SSO.
        logger.info(
            "SSO JIT: tenant %s allows jit_create but the token carried no "
            "email claim — rejecting (add email to the Clerk JWT template)",
            tenant.id,
        )
        return False

    role = sso_cfg.get("default_role") or "agent"
    if role not in _ALLOWED_ROLES:
        role = "agent"
    user = User(
        tenant_id=tenant.id,
        clerk_user_id=clerk_user_id,
        email=email,
        name=_extract_name(payload),
        role=role,
        is_active=True,
        agent_domains=[str(d) for d in (sso_cfg.get("default_agent_domains") or [])],
        manager_domains=[],
        is_tenant_admin=False,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Race: a concurrent login for the same identity won. The unique
        # constraint on clerk_user_id fired — the row now exists, which is
        # all the caller needs.
        await db.rollback()
        logger.info(
            "SSO JIT: concurrent create for clerk id %s resolved by peer",
            clerk_user_id,
        )
        return True
    logger.info(
        "SSO JIT: created user %s for clerk id %s on tenant %s (role=%s)",
        user.id,
        clerk_user_id,
        tenant.id,
        role,
    )
    return True


async def _resolve_tenant(
    db: AsyncSession,
    *,
    org_id: Optional[str],
    email: Optional[str],
) -> Optional[Tenant]:
    """Map a token to exactly one tenant via its ``features_enabled['sso']``.

    Org-id mapping wins over email-domain mapping. An ambiguous match
    (two tenants claim the same org id or domain) resolves to ``None`` —
    fail closed rather than drop a user into the wrong tenant. ``tenants``
    is a global RLS table, so this cross-tenant read needs no binding.
    """
    tenants = (await db.execute(select(Tenant))).scalars().all()

    if org_id:
        matches = [
            t
            for t in tenants
            if org_id in _sso_list(t, "clerk_org_ids")
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "SSO JIT: org_id %s maps to %d tenants — ambiguous, rejecting",
                org_id,
                len(matches),
            )
            return None

    if email and "@" in email:
        domain = email.rsplit("@", 1)[1].lower()
        matches = [
            t
            for t in tenants
            if domain in [d.lower() for d in _sso_list(t, "email_domains")]
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "SSO JIT: email domain %s maps to %d tenants — ambiguous, "
                "rejecting",
                domain,
                len(matches),
            )
            return None

    return None


def _sso_list(tenant: Tenant, key: str) -> List[str]:
    sso = (tenant.features_enabled or {}).get("sso") or {}
    val = sso.get(key)
    return [str(v) for v in val] if isinstance(val, list) else []


# ── Claim extraction ─────────────────────────────────────────────────
#
# Clerk's default session token carries neither email nor org unless the
# instance's JWT template is customised. We read the common shapes so the
# same code works whether the operator used Clerk's ``{{user.…}}`` /
# ``{{org.…}}`` shortcodes or a namespaced custom claim.


def _extract_email(payload: Dict[str, Any]) -> Optional[str]:
    for key in (
        "email",
        "email_address",
        "primary_email",
        "primary_email_address",
        "https://linda.app/email",
    ):
        val = payload.get(key)
        if isinstance(val, str) and "@" in val:
            return val.strip()
    return None


def _extract_org_id(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("org_id", "organization_id", "https://linda.app/org_id"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val.strip()
    return None


def _extract_name(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("name", "full_name"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    first = payload.get("first_name") or payload.get("given_name")
    last = payload.get("last_name") or payload.get("family_name")
    parts = [p for p in (first, last) if isinstance(p, str) and p.strip()]
    return " ".join(parts) if parts else None


def _mask_email(email: Optional[str]) -> str:
    if not email or "@" not in email:
        return "<none>"
    local, _, domain = email.partition("@")
    shown = local[:2] if len(local) > 2 else local[:1]
    return f"{shown}***@{domain}"
