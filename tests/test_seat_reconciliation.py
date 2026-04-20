"""Seat-reconciliation service tests.

Covers the reconcile_seats + reactivate_user logic with lightweight
in-memory tenant/user doubles, so we don't need Postgres.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from backend.app.services.seat_reconciliation import (
    SUSPENSION_REASON,
    reactivate_user,
    reconcile_seats,
    clear_reconciliation_if_under_cap,
)


@dataclass
class FakeUser:
    id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    is_active: bool
    created_at: datetime
    suspension_reason: Optional[str] = None


@dataclass
class FakeTenant:
    id: uuid.UUID
    seat_limit: int
    admin_seat_limit: int
    pending_seat_reconciliation: bool = False


class FakeDB:
    """Just enough to satisfy the reconcile + reactivate queries."""

    def __init__(self, users: List[FakeUser]):
        self._users = users

    async def execute(self, stmt):
        try:
            params = stmt.compile().params
        except Exception:
            params = {}

        # What kind of query? Peek at the compiled SQL.
        compiled_sql = str(stmt.compile()).lower()

        # Count-query path: return int count of matching users.
        if "count(" in compiled_sql:
            matches = self._filter_users(params, compiled_sql)
            return _CountResult(len(matches))

        # Otherwise assume it's a select(User).where(...) returning rows.
        matches = self._filter_users(params, compiled_sql)
        matches.sort(key=lambda u: u.created_at)
        return _ListResult(matches)

    async def get(self, model, user_id):
        for u in self._users:
            if u.id == user_id:
                return u
        return None

    def _filter_users(self, params, compiled_sql):
        rows = list(self._users)
        # Filter by tenant_id when present.
        tenant_id = None
        for key, val in params.items():
            if "tenant_id" in key and isinstance(val, uuid.UUID):
                tenant_id = val
                break
        if tenant_id is not None:
            rows = [u for u in rows if u.tenant_id == tenant_id]

        if "is_active" in compiled_sql:
            rows = [u for u in rows if u.is_active]

        if "'admin'" in compiled_sql or "'admin'" in str(params.values()) or any(
            isinstance(v, str) and v == "admin" for v in params.values()
        ):
            rows = [u for u in rows if u.role == "admin"]

        if SUSPENSION_REASON in str(params.values()):
            rows = [u for u in rows if u.suspension_reason == SUSPENSION_REASON]

        return rows


class _ListResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self):
        rows = self._rows
        class _S:
            def all(self_inner): return rows
        return _S()
    def scalar_one_or_none(self): return self._rows[0] if self._rows else None
    def scalar_one(self): return len(self._rows)


class _CountResult:
    def __init__(self, n): self._n = n
    def scalar_one(self): return self._n


def _mk_tenant(seat_limit=1, admin_seat_limit=1):
    return FakeTenant(id=uuid.uuid4(), seat_limit=seat_limit, admin_seat_limit=admin_seat_limit)


def _mk_user(tenant_id, role="agent", *, days_old=10, active=True):
    return FakeUser(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=role,
        is_active=active,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
    )


# ── reconcile_seats ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_no_op_when_under_cap():
    tenant = _mk_tenant(seat_limit=5, admin_seat_limit=1)
    users = [_mk_user(tenant.id, role="admin", days_old=30)]
    db = FakeDB(users)
    result = await reconcile_seats(db, tenant)
    assert result.suspended_user_ids == []
    assert result.suspended_admin_ids == []
    assert tenant.pending_seat_reconciliation is False
    assert users[0].is_active is True


@pytest.mark.asyncio
async def test_reconcile_suspends_newest_nonadmins():
    """Downgrade from 5 seats to 2. Admin + 4 agents → keep admin +
    oldest agent; suspend the 3 newest."""
    tenant = _mk_tenant(seat_limit=2, admin_seat_limit=1)
    admin = _mk_user(tenant.id, role="admin", days_old=30)
    agents = [
        _mk_user(tenant.id, role="agent", days_old=25),  # oldest — kept
        _mk_user(tenant.id, role="agent", days_old=20),
        _mk_user(tenant.id, role="agent", days_old=15),
        _mk_user(tenant.id, role="agent", days_old=5),   # newest — first out
    ]
    db = FakeDB([admin] + agents)

    result = await reconcile_seats(db, tenant)
    assert len(result.suspended_user_ids) == 3
    # The admin + the oldest agent stay active.
    assert admin.is_active is True
    assert agents[0].is_active is True
    # Newer three suspended with the right reason.
    for suspended in agents[1:]:
        assert suspended.is_active is False
        assert suspended.suspension_reason == SUSPENSION_REASON
    assert tenant.pending_seat_reconciliation is True


@pytest.mark.asyncio
async def test_reconcile_protects_acting_admin():
    """The admin who triggered the downgrade must never suspend
    themselves, even if they're the newest user."""
    tenant = _mk_tenant(seat_limit=2, admin_seat_limit=1)
    older_admin = _mk_user(tenant.id, role="admin", days_old=30)
    acting_admin = _mk_user(tenant.id, role="admin", days_old=1)  # brand new
    agent = _mk_user(tenant.id, role="agent", days_old=20)
    db = FakeDB([older_admin, acting_admin, agent])

    result = await reconcile_seats(db, tenant, protect_user_id=acting_admin.id)

    # Admin limit is 1; two admins; protected one stays, other gets bumped.
    assert acting_admin.is_active is True
    assert older_admin.id in result.suspended_admin_ids or older_admin.id in result.suspended_user_ids


@pytest.mark.asyncio
async def test_reconcile_enforces_admin_seat_limit():
    """Admin headcount can exceed admin_seat_limit even when total users
    fit — suspend extra admins."""
    tenant = _mk_tenant(seat_limit=10, admin_seat_limit=1)
    admins = [
        _mk_user(tenant.id, role="admin", days_old=30),
        _mk_user(tenant.id, role="admin", days_old=10),  # extra
    ]
    db = FakeDB(admins)

    result = await reconcile_seats(db, tenant)

    assert admins[0].is_active is True
    assert admins[1].is_active is False
    assert admins[1].id in result.suspended_admin_ids


@pytest.mark.asyncio
async def test_reconcile_is_idempotent():
    """Running twice doesn't keep suspending users once the caps fit."""
    tenant = _mk_tenant(seat_limit=2, admin_seat_limit=1)
    admin = _mk_user(tenant.id, role="admin", days_old=30)
    kept_agent = _mk_user(tenant.id, role="agent", days_old=20)
    db = FakeDB([admin, kept_agent])

    first = await reconcile_seats(db, tenant)
    second = await reconcile_seats(db, tenant)

    assert first.suspended_user_ids == []
    assert second.suspended_user_ids == []
    assert tenant.pending_seat_reconciliation is False


# ── reactivate_user ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reactivate_fails_when_already_active():
    tenant = _mk_tenant(seat_limit=5, admin_seat_limit=1)
    user = _mk_user(tenant.id, role="agent", days_old=1, active=True)
    db = FakeDB([user])
    with pytest.raises(ValueError):
        await reactivate_user(db, tenant, user)


@pytest.mark.asyncio
async def test_reactivate_under_cap_succeeds():
    tenant = _mk_tenant(seat_limit=3, admin_seat_limit=1)
    admin = _mk_user(tenant.id, role="admin", days_old=30)
    suspended = _mk_user(tenant.id, role="agent", days_old=5, active=False)
    suspended.suspension_reason = SUSPENSION_REASON
    db = FakeDB([admin, suspended])

    result = await reactivate_user(db, tenant, suspended)
    assert suspended.is_active is True
    assert suspended.suspension_reason is None
    assert result["suspended_swap_user_id"] is None


@pytest.mark.asyncio
async def test_reactivate_at_cap_without_swap_raises():
    tenant = _mk_tenant(seat_limit=2, admin_seat_limit=1)
    admin = _mk_user(tenant.id, role="admin", days_old=30)
    active_agent = _mk_user(tenant.id, role="agent", days_old=20)
    suspended = _mk_user(tenant.id, role="agent", days_old=5, active=False)
    suspended.suspension_reason = SUSPENSION_REASON
    db = FakeDB([admin, active_agent, suspended])

    with pytest.raises(ValueError, match="suspend_swap_id required"):
        await reactivate_user(db, tenant, suspended)


@pytest.mark.asyncio
async def test_reactivate_at_cap_with_swap_swaps_them():
    tenant = _mk_tenant(seat_limit=2, admin_seat_limit=1)
    admin = _mk_user(tenant.id, role="admin", days_old=30)
    active_agent = _mk_user(tenant.id, role="agent", days_old=20)
    suspended = _mk_user(tenant.id, role="agent", days_old=5, active=False)
    suspended.suspension_reason = SUSPENSION_REASON
    db = FakeDB([admin, active_agent, suspended])

    result = await reactivate_user(
        db, tenant, suspended, suspend_swap_id=active_agent.id
    )
    assert suspended.is_active is True
    assert active_agent.is_active is False
    assert active_agent.suspension_reason == SUSPENSION_REASON
    assert result["reactivated_user_id"] == str(suspended.id)
    assert result["suspended_swap_user_id"] == str(active_agent.id)


@pytest.mark.asyncio
async def test_reactivate_admin_requires_admin_swap():
    tenant = _mk_tenant(seat_limit=5, admin_seat_limit=1)
    existing_admin = _mk_user(tenant.id, role="admin", days_old=30)
    agent = _mk_user(tenant.id, role="agent", days_old=20)
    suspended_admin = _mk_user(tenant.id, role="admin", days_old=5, active=False)
    suspended_admin.suspension_reason = SUSPENSION_REASON
    db = FakeDB([existing_admin, agent, suspended_admin])

    # Swapping against a non-admin shouldn't free an admin seat.
    with pytest.raises(ValueError, match="must swap an active admin"):
        await reactivate_user(
            db, tenant, suspended_admin, suspend_swap_id=agent.id
        )


@pytest.mark.asyncio
async def test_reactivate_swap_must_differ_from_target():
    tenant = _mk_tenant(seat_limit=1, admin_seat_limit=1)
    admin = _mk_user(tenant.id, role="admin", days_old=30)
    suspended = _mk_user(tenant.id, role="agent", days_old=5, active=False)
    suspended.suspension_reason = SUSPENSION_REASON
    db = FakeDB([admin, suspended])
    with pytest.raises(ValueError):
        await reactivate_user(db, tenant, suspended, suspend_swap_id=suspended.id)


# ── clear_reconciliation_if_under_cap ────────────────────────────────


@pytest.mark.asyncio
async def test_clear_flips_flag_off_when_no_suspensions_and_under_cap():
    tenant = _mk_tenant(seat_limit=5, admin_seat_limit=1)
    tenant.pending_seat_reconciliation = True
    users = [_mk_user(tenant.id, role="admin", days_old=30)]
    db = FakeDB(users)
    await clear_reconciliation_if_under_cap(db, tenant)
    assert tenant.pending_seat_reconciliation is False


@pytest.mark.asyncio
async def test_clear_keeps_flag_on_while_suspensions_exist():
    tenant = _mk_tenant(seat_limit=5, admin_seat_limit=1)
    tenant.pending_seat_reconciliation = True
    admin = _mk_user(tenant.id, role="admin", days_old=30)
    suspended = _mk_user(tenant.id, role="agent", days_old=5, active=False)
    suspended.suspension_reason = SUSPENSION_REASON
    db = FakeDB([admin, suspended])
    await clear_reconciliation_if_under_cap(db, tenant)
    # Still pending because we have a tier_downgrade suspension outstanding.
    assert tenant.pending_seat_reconciliation is True
