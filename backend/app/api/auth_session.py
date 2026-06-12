"""Per-user session auth + user management API.

Endpoints:

* ``POST /auth/login`` — email + password → session JWT
* ``GET  /auth/me`` — the current principal's identity for the UI
* ``POST /auth/logout`` — no-op for stateless JWTs (client drops the token)
* ``GET  /users`` — list tenant users (admin only)
* ``POST /users`` — create user (admin only, seat-enforced)
* ``PATCH /users/{id}`` — update role / name / is_active (admin only)
* ``POST /users/{id}/set-password`` — admin resets a password
* ``DELETE /users/{id}`` — deactivate (admin only; never hard-delete)

Seat enforcement (on create):
* total active users ≤ ``tenant.seat_limit``
* active admins ≤ ``tenant.admin_seat_limit``
* at least one active admin must remain after any role change.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    hash_password,
    issue_session_token,
    require_role,
    require_scope,
    verify_password,
)
from backend.app.db import get_db
from backend.app.models import User
from backend.app.services.audit_log import audit_log

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class LoginOut(BaseModel):
    token: str
    user: "UserOut"


_DOMAIN_VALUES = {"sales", "customer_service", "it_support", "generic"}


class UserOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    name: Optional[str] = None
    role: str
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    # ── Motion scopes (added in PR motion-assignment-admin-ui) ──────────
    # Mirror the columns added by migration ``dom_001``. Driven by the
    # Settings → User Management grid. Empty arrays are valid (a pure
    # tenant admin who takes no calls and manages no motion is a real
    # role at small companies).
    agent_domains: List[str] = []
    manager_domains: List[str] = []
    is_tenant_admin: bool = False

    model_config = {"from_attributes": True}


# Renamed from MeOut → AuthMeOut so the OpenAPI client doesn't collide
# with backend.app.api.me.MeOut (different shape, same legacy name).
class AuthMeOut(BaseModel):
    user: Optional[UserOut]
    tenant_id: uuid.UUID
    role: str
    source: str  # "session" | "api_key"


class UserCreate(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    role: Literal["agent", "manager", "admin"] = "agent"
    password: str = Field(..., min_length=8, max_length=200)
    # Motion scopes — optional at create time. When omitted, the
    # tenant's ``default_domain`` is used as the agent motion for
    # ``role=agent`` and the manager motion for ``role=manager``.
    # An empty list explicitly grants nothing for that slot.
    agent_domains: Optional[List[str]] = None
    manager_domains: Optional[List[str]] = None
    is_tenant_admin: Optional[bool] = None


class UserPatch(BaseModel):
    name: Optional[str] = None
    role: Optional[Literal["agent", "manager", "admin"]] = None
    is_active: Optional[bool] = None
    agent_domains: Optional[List[str]] = None
    manager_domains: Optional[List[str]] = None
    is_tenant_admin: Optional[bool] = None


def _validate_domain_list(value: Optional[List[str]], field: str) -> List[str]:
    """Reject anything outside the canonical vocabulary at the API edge.

    A typo like ``"customer-service"`` (kebab-cased) silently fails the
    ``can_manage_domain`` check downstream and produces "I should see the
    CS tab but I don't" tickets. Catching at the boundary makes the
    failure mode loud.
    """
    if value is None:
        return []
    cleaned: List[str] = []
    for v in value:
        if not isinstance(v, str):
            raise HTTPException(
                status_code=422,
                detail=f"{field}: domains must be strings",
            )
        v = v.strip()
        if v not in _DOMAIN_VALUES:
            raise HTTPException(
                status_code=422,
                detail=f"{field}: {v!r} is not a known domain",
            )
        if v not in cleaned:
            cleaned.append(v)
    return cleaned


class SetPasswordIn(BaseModel):
    password: str = Field(..., min_length=8, max_length=200)


LoginOut.model_rebuild()


# ── Login / me / logout ───────────────────────────────────────────────


@router.post("/auth/login", response_model=LoginOut)
async def login(
    body: LoginIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Email + password → session JWT.

    Info-leak posture:

    * Unknown email → generic 401 "Invalid credentials".
    * Wrong password → generic 401 "Invalid credentials".
    * Correct password BUT user is inactive/suspended → 403 with a
      specific message. An attacker who correctly guessed both halves of
      the credential already owns the account; telling them "suspended"
      doesn't help them further, while it meaningfully helps the
      legitimate user understand what happened.
    """
    stmt = select(User).where(User.email == body.email.lower())
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        reason = user.suspension_reason or "deactivated"
        detail = (
            "Account suspended by a subscription downgrade. "
            "Contact your administrator to be reactivated."
            if reason == "tier_downgrade"
            else "Account is not active. Contact your administrator."
        )
        raise HTTPException(status_code=403, detail=detail)

    user.last_login_at = datetime.now(timezone.utc)
    token = issue_session_token(user)
    return LoginOut(token=token, user=UserOut.model_validate(user))


@router.get("/auth/me", response_model=AuthMeOut)
async def me(
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Return the current caller's identity — used by the UI to decide
    which nav items and pages to show. For API-key callers, ``user`` is
    ``None`` and ``role`` is ``admin`` (tenant-wide scope)."""
    return AuthMeOut(
        user=UserOut.model_validate(principal.user) if principal.user else None,
        tenant_id=principal.tenant.id,
        role=principal.role,
        source=principal.source,
    )


@router.post("/auth/logout", status_code=204)
async def logout(
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Stateless logout — the server drops nothing; the client should
    discard the token. Kept as a route so UIs can POST here and assume a
    logout has happened."""
    return None


# ── User management (admin-only) ──────────────────────────────────────


async def _active_user_counts(db: AsyncSession, tenant_id: uuid.UUID):
    """Return (total_active, active_admins) for the tenant."""
    total = (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == tenant_id, User.is_active.is_(True))
        )
    ).scalar_one()
    admins = (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.role == "admin",
            )
        )
    ).scalar_one()
    return int(total), int(admins)


@router.get("/users", response_model=List[UserOut])
async def list_users(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
):
    """List all users in the tenant. Admin only."""
    stmt = select(User).where(User.tenant_id == principal.tenant.id)
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    stmt = stmt.order_by(User.created_at.asc())
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


class UserLookupOut(BaseModel):
    """Tiny shape — id + display name only, exposed to non-admins for
    assignee pickers in /action-items, comment @mention, etc. We do not
    leak email or role here so a manager can't enumerate the tenant's
    admin set."""

    id: uuid.UUID
    name: Optional[str] = None


@router.get("/users/lookup", response_model=List[UserLookupOut])
async def list_users_lookup(
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Non-admin "who can I assign this to" picker. Returns active tenant
    users as id + display name only — every authenticated principal in
    the tenant can call this. Backed by ``users`` but stripped of email,
    role, last_login_at, etc. so it doesn't leak admin enumeration."""
    stmt = (
        select(User.id, User.name, User.email)
        .where(
            User.tenant_id == principal.tenant.id,
            User.is_active.is_(True),
        )
        .order_by(User.created_at.asc())
    )
    rows = (await db.execute(stmt)).all()
    # Fall back to the email-local-part so the picker label is never blank
    # — but we still strip the @domain so the picker isn't a directory.
    out: List[UserLookupOut] = []
    for row in rows:
        display = row[1] or (row[2].split("@", 1)[0] if row[2] else None)
        out.append(UserLookupOut(id=row[0], name=display))
    return out


@router.post(
    "/users",
    response_model=UserOut,
    status_code=201,
    dependencies=[Depends(require_scope("users:write"))],
)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
):
    """Create a new user under the current tenant. Seat-enforced."""
    email = body.email.lower()

    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email already in use")

    tenant = principal.tenant
    total, admins = await _active_user_counts(db, tenant.id)

    if total + 1 > (tenant.seat_limit or 1):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Seat limit reached ({tenant.seat_limit}). Upgrade your "
                "subscription or deactivate a user first."
            ),
        )
    if body.role == "admin" and admins + 1 > (tenant.admin_seat_limit or 1):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Admin seat limit reached ({tenant.admin_seat_limit}). "
                "Promote a manager on a higher tier or demote an existing admin."
            ),
        )

    # Defaulting rules when motion scopes aren't supplied: agent gets
    # the tenant's default_domain as their agent motion, manager gets it
    # as both agent + manager motions, admin gets it as everything plus
    # ``is_tenant_admin=True``. Matches the ``dom_001`` backfill behaviour
    # so an admin who clicks "Invite" with no extra fields gets the same
    # result as the existing seed data.
    default_domain = tenant.default_domain or "sales"
    if body.agent_domains is None:
        agent_domains = (
            [default_domain] if body.role in ("agent", "manager", "admin") else []
        )
    else:
        agent_domains = _validate_domain_list(body.agent_domains, "agent_domains")
    if body.manager_domains is None:
        manager_domains = (
            [default_domain] if body.role in ("manager", "admin") else []
        )
    else:
        manager_domains = _validate_domain_list(body.manager_domains, "manager_domains")
    is_tenant_admin = (
        body.is_tenant_admin
        if body.is_tenant_admin is not None
        else (body.role == "admin")
    )

    user = User(
        tenant_id=tenant.id,
        email=email,
        name=body.name,
        role=body.role,
        password_hash=hash_password(body.password),
        is_active=True,
        agent_domains=agent_domains,
        manager_domains=manager_domains,
        is_tenant_admin=is_tenant_admin,
    )
    db.add(user)
    await db.flush()
    await audit_log(
        db,
        principal,
        action="user.created",
        resource_type="user",
        resource_id=str(user.id),
        after={
            "email": user.email,
            "role": user.role,
            "name": user.name,
            "agent_domains": agent_domains,
            "manager_domains": manager_domains,
            "is_tenant_admin": is_tenant_admin,
        },
    )
    return user


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    dependencies=[Depends(require_scope("users:write"))],
)
async def patch_user(
    user_id: uuid.UUID,
    body: UserPatch,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
):
    """Update a user's name / role / active state. Tenant-scoped.

    Guard rails:
    * You can't demote or deactivate the last active admin in the tenant.
    * Promoting to admin respects ``admin_seat_limit``.
    """
    user = await db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="User not found")

    before = {"role": user.role, "is_active": user.is_active, "name": user.name}
    updates = body.model_dump(exclude_none=True)
    total, admins = await _active_user_counts(db, principal.tenant.id)

    new_role = updates.get("role", user.role)
    new_active = updates.get("is_active", user.is_active)

    # Guard: never strand a tenant with zero admins.
    demoted = user.role == "admin" and new_role != "admin"
    deactivated = user.is_active and not new_active
    if (demoted or deactivated) and admins <= 1 and user.role == "admin":
        raise HTTPException(
            status_code=400,
            detail="At least one active admin must remain on the tenant.",
        )

    # Guard: admin promotion respects admin_seat_limit.
    promoted_to_admin = user.role != "admin" and new_role == "admin" and new_active
    if promoted_to_admin and admins + 1 > (principal.tenant.admin_seat_limit or 1):
        raise HTTPException(
            status_code=400,
            detail=f"Admin seat limit reached ({principal.tenant.admin_seat_limit}).",
        )

    # Reactivation respects total seat_limit.
    reactivated = not user.is_active and new_active
    if reactivated and total + 1 > (principal.tenant.seat_limit or 1):
        raise HTTPException(
            status_code=400, detail=f"Seat limit reached ({principal.tenant.seat_limit})."
        )

    # Validate domain lists before assigning so a bad value rejects the
    # whole patch rather than partially applying.
    if "agent_domains" in updates:
        updates["agent_domains"] = _validate_domain_list(
            updates["agent_domains"], "agent_domains"
        )
    if "manager_domains" in updates:
        updates["manager_domains"] = _validate_domain_list(
            updates["manager_domains"], "manager_domains"
        )

    # Guard: don't strip the last tenant admin via ``is_tenant_admin``
    # toggle (separate from the role guard above so an admin who took
    # themselves off the admin role months ago still trips this).
    if updates.get("is_tenant_admin") is False and user.is_tenant_admin:
        tenant_admins = (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(
                    User.tenant_id == principal.tenant.id,
                    User.is_active.is_(True),
                    User.is_tenant_admin.is_(True),
                )
            )
        ).scalar_one()
        if int(tenant_admins) <= 1:
            raise HTTPException(
                status_code=400,
                detail="At least one tenant admin must remain.",
            )

    for k, v in updates.items():
        setattr(user, k, v)
    await db.flush()
    await audit_log(
        db,
        principal,
        action="user.updated",
        resource_type="user",
        resource_id=str(user.id),
        before=before,
        after={
            "role": user.role,
            "is_active": user.is_active,
            "name": user.name,
            "agent_domains": user.agent_domains,
            "manager_domains": user.manager_domains,
            "is_tenant_admin": user.is_tenant_admin,
        },
    )
    return user


@router.post(
    "/users/{user_id}/set-password",
    status_code=204,
    dependencies=[Depends(require_scope("users:write"))],
)
async def set_password(
    user_id: uuid.UUID,
    body: SetPasswordIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
):
    """Admin-driven password reset. Doesn't send email — UI surfaces the
    new password once or asks the admin to communicate it out-of-band."""
    user = await db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(body.password)
    return None


@router.delete("/users/{user_id}", status_code=204)
async def deactivate_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
):
    """Soft-delete via ``is_active=False``. Never hard-deletes (would
    break foreign-key-referenced audit columns like ``pinned_by_user_id``)."""
    user = await db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="User not found")

    _, admins = await _active_user_counts(db, principal.tenant.id)
    if user.role == "admin" and admins <= 1 and user.is_active:
        raise HTTPException(
            status_code=400,
            detail="At least one active admin must remain on the tenant.",
        )
    user.is_active = False
    return None


# ── Seat reconciliation (admin-only) ─────────────────────────────────


class SeatReconciliationOut(BaseModel):
    pending: bool
    seat_limit: int
    admin_seat_limit: int
    active_users: int
    active_admins: int
    suspended_users: List[UserOut]


class ReactivateIn(BaseModel):
    # When the tenant is already at cap, the admin must pick an
    # existing active user to suspend in place of the one coming back.
    # Forces the trade-off to be deliberate.
    suspend_swap_user_id: Optional[uuid.UUID] = None


@router.get(
    "/admin/seat-reconciliation",
    response_model=SeatReconciliationOut,
)
async def get_seat_reconciliation(
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
):
    """Show the tenant's current seat headcount + anyone auto-suspended
    by a tier downgrade. Drives the admin-UI banner."""
    from backend.app.services.seat_reconciliation import SUSPENSION_REASON

    total_stmt = (
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == principal.tenant.id, User.is_active.is_(True))
    )
    admin_stmt = (
        select(func.count())
        .select_from(User)
        .where(
            User.tenant_id == principal.tenant.id,
            User.is_active.is_(True),
            User.role == "admin",
        )
    )
    suspended_stmt = (
        select(User)
        .where(
            User.tenant_id == principal.tenant.id,
            User.suspension_reason == SUSPENSION_REASON,
        )
        .order_by(User.created_at.asc())
    )
    total = int((await db.execute(total_stmt)).scalar_one())
    admins = int((await db.execute(admin_stmt)).scalar_one())
    suspended = list((await db.execute(suspended_stmt)).scalars().all())

    return SeatReconciliationOut(
        pending=bool(principal.tenant.pending_seat_reconciliation),
        seat_limit=int(principal.tenant.seat_limit or 1),
        admin_seat_limit=int(principal.tenant.admin_seat_limit or 1),
        active_users=total,
        active_admins=admins,
        suspended_users=[UserOut.model_validate(u) for u in suspended],
    )


@router.post("/users/{user_id}/reactivate", response_model=UserOut)
async def reactivate_user_endpoint(
    user_id: uuid.UUID,
    body: ReactivateIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
):
    """Reactivate a suspended user.

    If the tenant is already at cap, the admin must set
    ``suspend_swap_user_id`` on the body — the picked active user is
    suspended to make room. This enforces a deliberate trade instead
    of silently expanding the tenant past its subscription.
    """
    from backend.app.services.seat_reconciliation import reactivate_user

    user = await db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        await reactivate_user(
            db,
            principal.tenant,
            user,
            suspend_swap_id=body.suspend_swap_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return UserOut.model_validate(user)
