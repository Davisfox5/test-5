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
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    hash_password,
    issue_session_token,
    require_role,
    verify_password,
)
from backend.app.db import get_db
from backend.app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class LoginOut(BaseModel):
    token: str
    user: "UserOut"


class UserOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    name: Optional[str] = None
    role: str
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MeOut(BaseModel):
    user: Optional[UserOut]
    tenant_id: uuid.UUID
    role: str
    source: str  # "session" | "api_key"


class UserCreate(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    role: Literal["agent", "manager", "admin"] = "agent"
    password: str = Field(..., min_length=8, max_length=200)


class UserPatch(BaseModel):
    name: Optional[str] = None
    role: Optional[Literal["agent", "manager", "admin"]] = None
    is_active: Optional[bool] = None


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

    Does not leak whether an email exists: invalid credentials always
    return a generic 401.
    """
    stmt = select(User).where(User.email == body.email.lower())
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user.last_login_at = datetime.now(timezone.utc)
    token = issue_session_token(user)
    return LoginOut(token=token, user=UserOut.model_validate(user))


@router.get("/auth/me", response_model=MeOut)
async def me(
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Return the current caller's identity — used by the UI to decide
    which nav items and pages to show. For API-key callers, ``user`` is
    ``None`` and ``role`` is ``admin`` (tenant-wide scope)."""
    return MeOut(
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


@router.post("/users", response_model=UserOut, status_code=201)
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

    user = User(
        tenant_id=tenant.id,
        email=email,
        name=body.name,
        role=body.role,
        password_hash=hash_password(body.password),
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
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

    for k, v in updates.items():
        setattr(user, k, v)
    return user


@router.post("/users/{user_id}/set-password", status_code=204)
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
