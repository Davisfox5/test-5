"""Seat reconciliation — auto-suspend excess users on tier downgrade.

When ``apply_tier`` sets a new ``seat_limit`` / ``admin_seat_limit``, the
active user count may exceed the new caps. This module brings headcount
back into compliance by suspending the newest excess users with
``suspension_reason="tier_downgrade"`` and flagging the tenant with
``pending_seat_reconciliation=True`` so the admin UI can surface the
banner.

Rules:

* Suspend **non-admin** users first when ``active_users > seat_limit``.
  Ordering: newest by ``created_at`` gets suspended first (the admin
  can always swap via ``reactivate_user``).
* If admins themselves exceed ``admin_seat_limit``, suspend newest
  admins too — but **never** the one we're told to protect (the admin
  currently calling the API, passed in via ``protect_user_id``).
* ``pending_seat_reconciliation`` is True whenever any user has
  ``suspension_reason="tier_downgrade"``. Cleared in ``reactivate_user``
  as soon as counts fit.

Public API:

* ``reconcile_seats(db, tenant, protect_user_id=None)`` — apply caps in
  place. Returns a summary for the webhook/endpoint response body.
* ``reactivate_user(db, tenant, user, suspend_swap_id=None)`` — bring a
  suspended user back online. Caller must pick a swap victim (another
  active user) if the tenant is already at cap.
* ``clear_reconciliation_if_under_cap(db, tenant)`` — called after any
  seat mutation to flip the banner flag off once counts fit.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Tenant, User

logger = logging.getLogger(__name__)


SUSPENSION_REASON = "tier_downgrade"


@dataclass
class ReconcileResult:
    """What reconcile_seats did. Populated for the API response."""

    tenant_id: uuid.UUID
    suspended_user_ids: List[uuid.UUID] = field(default_factory=list)
    suspended_admin_ids: List[uuid.UUID] = field(default_factory=list)
    active_users_after: int = 0
    active_admins_after: int = 0
    pending: bool = False


async def _active_users(
    db: AsyncSession, tenant_id: uuid.UUID, admin_only: bool = False
) -> List[User]:
    stmt = (
        select(User)
        .where(User.tenant_id == tenant_id, User.is_active.is_(True))
        .order_by(User.created_at.asc())
    )
    if admin_only:
        stmt = stmt.where(User.role == "admin")
    return list((await db.execute(stmt)).scalars().all())


async def reconcile_seats(
    db: AsyncSession,
    tenant: Tenant,
    *,
    protect_user_id: Optional[uuid.UUID] = None,
) -> ReconcileResult:
    """Bring active counts under the tenant's current caps.

    Idempotent: calling when already under cap flips the banner off and
    returns an empty result.
    """
    result = ReconcileResult(tenant_id=tenant.id)

    seat_limit = max(1, int(tenant.seat_limit or 1))
    admin_seat_limit = max(1, int(tenant.admin_seat_limit or 1))

    actives = await _active_users(db, tenant.id)
    # Pin the protected user (if any) to the front so they survive trimming.
    if protect_user_id is not None:
        actives.sort(key=lambda u: (u.id != protect_user_id, u.created_at))
    else:
        actives.sort(key=lambda u: u.created_at)

    # Partition into admins vs. non-admins; keep the creation-order first
    # ``seat_limit`` overall.
    kept: List[User] = []
    suspended: List[User] = []
    for user in actives:
        if len(kept) < seat_limit:
            kept.append(user)
        else:
            suspended.append(user)

    # Second pass: enforce admin_seat_limit *within* the kept set. If
    # too many admins made the cut, newest-created admins become
    # candidates for suspension. Protected user is still immune.
    kept_admins = [u for u in kept if u.role == "admin"]
    if len(kept_admins) > admin_seat_limit:
        # Sort admins so protected first, then oldest-first; newest get
        # bumped past the admin cap.
        kept_admins.sort(
            key=lambda u: (
                u.id != protect_user_id if protect_user_id is not None else False,
                u.created_at,
            )
        )
        admin_overflow = kept_admins[admin_seat_limit:]
        # Anyone in ``admin_overflow`` (besides the protected user) gets
        # suspended. We don't silently demote — that'd be a different,
        # more invasive action the admin should choose explicitly.
        for a in admin_overflow:
            kept.remove(a)
            suspended.append(a)

    # Apply the suspensions.
    for user in suspended:
        if user.id == protect_user_id:
            # Protected admin — never suspend (even if they were newest).
            continue
        if user.is_active:
            user.is_active = False
            user.suspension_reason = SUSPENSION_REASON
        if user.role == "admin":
            result.suspended_admin_ids.append(user.id)
        else:
            result.suspended_user_ids.append(user.id)

    # Counts reflect the state after suspensions.
    total_active = sum(1 for u in kept if u.is_active or u.id == protect_user_id)
    admin_active = sum(
        1 for u in kept if u.role == "admin" and (u.is_active or u.id == protect_user_id)
    )
    result.active_users_after = total_active
    result.active_admins_after = admin_active

    pending = bool(result.suspended_user_ids or result.suspended_admin_ids)
    tenant.pending_seat_reconciliation = pending
    result.pending = pending

    return result


async def _count_actives(
    db: AsyncSession, tenant_id: uuid.UUID
) -> tuple[int, int]:
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


async def reactivate_user(
    db: AsyncSession,
    tenant: Tenant,
    user: User,
    *,
    suspend_swap_id: Optional[uuid.UUID] = None,
) -> dict:
    """Bring a suspended user back online.

    If the tenant is already at cap, the caller must pick a
    ``suspend_swap_id`` pointing at an *active* user who will be
    suspended to make room. This forces the admin to make the trade
    explicitly rather than silently sliding past the cap.
    """
    if user.tenant_id != tenant.id:
        raise ValueError("user does not belong to tenant")
    if user.is_active:
        raise ValueError("user is already active")

    total, admins = await _count_actives(db, tenant.id)
    at_total_cap = total >= (tenant.seat_limit or 1)
    at_admin_cap = user.role == "admin" and admins >= (tenant.admin_seat_limit or 1)

    swap_user: Optional[User] = None
    if at_total_cap or at_admin_cap:
        if suspend_swap_id is None:
            raise ValueError("at cap — suspend_swap_id required to make room")
        swap_user = await db.get(User, suspend_swap_id)
        if (
            swap_user is None
            or swap_user.tenant_id != tenant.id
            or not swap_user.is_active
            or swap_user.id == user.id
        ):
            raise ValueError("suspend_swap_id must reference a different active user")
        # If only the admin cap is exceeded, the swap target must be an admin.
        if at_admin_cap and swap_user.role != "admin":
            raise ValueError("must swap an active admin to free an admin seat")
        swap_user.is_active = False
        swap_user.suspension_reason = SUSPENSION_REASON

    user.is_active = True
    user.suspension_reason = None

    await clear_reconciliation_if_under_cap(db, tenant)

    return {
        "reactivated_user_id": str(user.id),
        "suspended_swap_user_id": str(swap_user.id) if swap_user else None,
        "pending_seat_reconciliation": tenant.pending_seat_reconciliation,
    }


async def clear_reconciliation_if_under_cap(
    db: AsyncSession, tenant: Tenant
) -> None:
    """Flip the banner off once active counts are back within caps."""
    total, admins = await _count_actives(db, tenant.id)
    under_total = total <= (tenant.seat_limit or 1)
    under_admin = admins <= (tenant.admin_seat_limit or 1)

    # Also check that no ``tier_downgrade``-suspended users remain — if
    # admins deactivated someone manually we might be back under cap
    # while still having suspensions to resolve.
    stmt = select(func.count()).select_from(User).where(
        User.tenant_id == tenant.id,
        User.suspension_reason == SUSPENSION_REASON,
    )
    suspended_count = int((await db.execute(stmt)).scalar_one())

    tenant.pending_seat_reconciliation = (
        (not under_total) or (not under_admin) or suspended_count > 0
    )
