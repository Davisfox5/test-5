"""Admin endpoints for motion-assignment management.

Surfaces two operations needed by Settings → User Management that
don't fit cleanly on the existing ``/users`` CRUD or ``/admin/*``
routers:

* ``POST /admin/users/import`` — CSV bulk-import of new users with
  motion assignments. Used when a customer is onboarding 30+ people
  and the row-at-a-time invite flow would be a slog.
* ``PUT /admin/tenant/default-motion`` — set the tenant's
  ``default_domain`` (``sales`` / ``customer_service`` / ``it_support``
  / ``generic``). Drives the invite-time defaulting in
  ``POST /users`` and the legacy backfill behaviour.

Both routes require ``require_role("admin")``. CSV parsing is hand-rolled
(``csv`` stdlib) to avoid pulling in pandas just for this; the file is
expected to be small (we cap at 200 rows).
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    CANONICAL_DOMAINS,
    hash_password,
    require_role,
)
from backend.app.db import get_db
from backend.app.models import Tenant, User
from backend.app.services.audit_log import audit_log

logger = logging.getLogger(__name__)

router = APIRouter()


_MAX_IMPORT_ROWS = 200
_DOMAIN_SET = set(CANONICAL_DOMAINS)


# ── Tenant default motion ─────────────────────────────────────────────


class TenantDefaultMotionIn(BaseModel):
    default_domain: Literal["sales", "customer_service", "it_support", "generic"]


class TenantDefaultMotionOut(BaseModel):
    default_domain: str


@router.get(
    "/admin/tenant/default-motion",
    response_model=TenantDefaultMotionOut,
)
async def get_tenant_default_motion(
    principal: AuthPrincipal = Depends(require_role("admin")),
) -> TenantDefaultMotionOut:
    """Read the tenant's primary motion. Drives invite defaulting."""
    return TenantDefaultMotionOut(
        default_domain=principal.tenant.default_domain or "sales"
    )


@router.put(
    "/admin/tenant/default-motion",
    response_model=TenantDefaultMotionOut,
)
async def set_tenant_default_motion(
    body: TenantDefaultMotionIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
) -> TenantDefaultMotionOut:
    """Set the tenant's primary motion.

    Doesn't retroactively change anyone's ``agent_domains`` — that would
    surprise customers who already have a mixed-motion setup. Only new
    invites that omit ``agent_domains`` pick up the new default.
    """
    db_tenant = await db.get(Tenant, principal.tenant.id)
    if db_tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    before = db_tenant.default_domain
    db_tenant.default_domain = body.default_domain
    await audit_log(
        db,
        principal,
        action="tenant.default_motion_set",
        resource_type="tenant",
        resource_id=str(db_tenant.id),
        before={"default_domain": before},
        after={"default_domain": body.default_domain},
    )
    await db.commit()
    return TenantDefaultMotionOut(default_domain=db_tenant.default_domain)


# ── CSV bulk import ───────────────────────────────────────────────────


class UserImportRowResult(BaseModel):
    """One row's outcome. ``user_id`` populated on success; ``error``
    populated on failure. Always non-fatal — one bad row doesn't abort
    the import."""

    line_number: int
    email: Optional[str] = None
    user_id: Optional[uuid.UUID] = None
    error: Optional[str] = None


class UserImportSummary(BaseModel):
    total_rows: int
    created: int
    skipped: int
    rows: List[UserImportRowResult]


def _parse_domain_list(raw: str, field: str) -> List[str]:
    """Parse a pipe-delimited or semicolon-delimited motion list.

    CSV cells can't reliably hold commas (the row delimiter), so the
    spreadsheet format is ``"sales|customer_service"`` or
    ``"sales;customer_service"``. Unknown values raise — the caller
    catches and records the line's error.
    """
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.split("|") for p in chunk.split(";")]
    out: List[str] = []
    for p in parts:
        if not p:
            continue
        if p not in _DOMAIN_SET:
            raise ValueError(f"{field}: unknown domain {p!r}")
        if p not in out:
            out.append(p)
    return out


def _parse_bool(raw: str) -> bool:
    """Permissive boolean parse. Accepts ``true / yes / 1 / y`` (any case)."""
    return raw.strip().lower() in {"true", "yes", "1", "y", "t"}


@router.post(
    "/admin/users/import",
    response_model=UserImportSummary,
)
async def import_users(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
) -> UserImportSummary:
    """Bulk-create users from a CSV.

    Expected columns (header row required, case-insensitive):

    * ``email`` (required)
    * ``name``
    * ``role`` (agent | manager | admin; default ``agent``)
    * ``password`` (required; admin's responsibility to communicate
      out-of-band — we don't email them from this endpoint)
    * ``agent_domains`` (pipe-delimited, e.g.
      ``"sales|customer_service"``)
    * ``manager_domains`` (same shape)
    * ``is_tenant_admin`` (true|false; default false)

    Each row is processed independently — one bad row doesn't abort the
    import; its error is recorded in the response so the admin can fix
    and re-upload only the offenders. Hard caps the upload at
    ``_MAX_IMPORT_ROWS`` rows to keep this from becoming a back-door
    bulk-create the seat-limit guard rails don't fully cover.

    Seat enforcement: every successful row counts against
    ``tenant.seat_limit``. When the cap would be exceeded mid-file, the
    remaining rows are skipped with a ``seat limit reached`` error.
    """
    if file is None:
        raise HTTPException(status_code=422, detail="No file provided")

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=422,
            detail="CSV must be UTF-8 encoded.",
        )

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(status_code=422, detail="Empty CSV")

    # Lowercase the headers so the import is case-insensitive.
    field_map = {name.strip().lower(): name for name in reader.fieldnames}
    required = {"email", "password"}
    missing = required - set(field_map.keys())
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"CSV missing required columns: {sorted(missing)}",
        )

    rows: List[UserImportRowResult] = []
    created = 0
    skipped = 0
    total = 0

    tenant = principal.tenant
    seat_limit = tenant.seat_limit or 1
    admin_seat_limit = tenant.admin_seat_limit or 1

    active_count = (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == tenant.id, User.is_active.is_(True))
        )
    ).scalar_one()
    active_admin_count = (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.tenant_id == tenant.id,
                User.is_active.is_(True),
                User.is_tenant_admin.is_(True),
            )
        )
    ).scalar_one()
    active = int(active_count)
    admins = int(active_admin_count)

    for line_number, row in enumerate(reader, start=2):  # 2 = first data row
        total += 1
        if total > _MAX_IMPORT_ROWS:
            rows.append(
                UserImportRowResult(
                    line_number=line_number,
                    error=(
                        f"Skipped: import is capped at "
                        f"{_MAX_IMPORT_ROWS} rows per file."
                    ),
                )
            )
            skipped += 1
            continue

        try:
            email = (row.get(field_map["email"]) or "").strip().lower()
            password = row.get(field_map["password"]) or ""
            if not email:
                raise ValueError("email is required")
            if "@" not in email:
                raise ValueError("email looks invalid")
            if len(password) < 8:
                raise ValueError("password must be >= 8 chars")

            name = (row.get(field_map.get("name", "")) or "").strip() or None
            role = (row.get(field_map.get("role", "")) or "agent").strip().lower()
            if role not in {"agent", "manager", "admin"}:
                raise ValueError(f"unknown role {role!r}")

            agent_domains_raw = row.get(field_map.get("agent_domains", "")) or ""
            manager_domains_raw = row.get(field_map.get("manager_domains", "")) or ""
            agent_domains = _parse_domain_list(agent_domains_raw, "agent_domains")
            manager_domains = _parse_domain_list(manager_domains_raw, "manager_domains")
            is_tenant_admin_raw = (
                row.get(field_map.get("is_tenant_admin", "")) or ""
            )
            is_tenant_admin = (
                _parse_bool(is_tenant_admin_raw)
                if is_tenant_admin_raw
                else (role == "admin")
            )

            # Defaulting when columns blank but role implies coverage —
            # mirrors the POST /users behaviour for parity.
            default_domain = tenant.default_domain or "sales"
            if not agent_domains and role in ("agent", "manager", "admin"):
                agent_domains = [default_domain]
            if not manager_domains and role in ("manager", "admin"):
                manager_domains = [default_domain]

            existing = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if existing is not None:
                raise ValueError("email already exists on this platform")

            if active + 1 > seat_limit:
                raise ValueError(f"seat limit reached ({seat_limit})")
            if is_tenant_admin and admins + 1 > admin_seat_limit:
                raise ValueError(
                    f"admin seat limit reached ({admin_seat_limit})"
                )

            user = User(
                tenant_id=tenant.id,
                email=email,
                name=name,
                role=role,
                password_hash=hash_password(password),
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
                action="user.imported",
                resource_type="user",
                resource_id=str(user.id),
                after={
                    "email": user.email,
                    "role": user.role,
                    "agent_domains": agent_domains,
                    "manager_domains": manager_domains,
                    "is_tenant_admin": is_tenant_admin,
                    "source_line": line_number,
                },
            )
            rows.append(
                UserImportRowResult(
                    line_number=line_number,
                    email=email,
                    user_id=user.id,
                )
            )
            created += 1
            active += 1
            if is_tenant_admin:
                admins += 1
        except ValueError as e:
            rows.append(
                UserImportRowResult(
                    line_number=line_number,
                    email=email if "email" in locals() else None,
                    error=str(e),
                )
            )
            skipped += 1
        except Exception:
            logger.exception("Unexpected CSV-import failure on line %d", line_number)
            rows.append(
                UserImportRowResult(
                    line_number=line_number,
                    email=email if "email" in locals() else None,
                    error="unexpected server error; see logs",
                )
            )
            skipped += 1

    if created:
        await db.commit()
    else:
        await db.rollback()

    return UserImportSummary(
        total_rows=total,
        created=created,
        skipped=skipped,
        rows=rows,
    )
