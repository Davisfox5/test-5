"""Audit log helper.

Every mutating endpoint is expected to emit a row via :func:`audit_log`
right after a successful DB write. The shape lines up with the
:class:`backend.app.models.AuditLog` columns:

* ``action`` — dot-namespaced verb, e.g. ``"interaction.deleted"``.
* ``resource_type`` — kebab-or-snake type name, e.g. ``"interaction"``,
  ``"webhook"``, ``"user"``.
* ``resource_id`` — string id of the row that changed (UUID or synthetic).
* ``before`` / ``after`` — JSONB snapshots. Pre-existing
  :class:`TenantDataOpsLog` rows continue to be written from
  ``backend/app/api/gdpr.py`` *and* are mirrored into ``AuditLog`` so
  the unified log is still complete.

We deliberately keep this an *explicit call* per endpoint rather than a
sniffing middleware. A middleware that introspects ``before``/``after``
would either need every endpoint to re-fetch the row, or (worse)
guess from the request body — both of which are more brittle than
just having endpoints describe their own changes.

The helper is best-effort: a failed audit write must not flunk the
business operation. We log at WARN and move on.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Mapping, Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal
from backend.app.models import AuditLog

logger = logging.getLogger(__name__)


# ── Public API ───────────────────────────────────────────────────────


async def audit_log(
    db: AsyncSession,
    principal: AuthPrincipal,
    *,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    before: Optional[Mapping[str, Any]] = None,
    after: Optional[Mapping[str, Any]] = None,
    request: Optional[Request] = None,
    extra_meta: Optional[Mapping[str, Any]] = None,
) -> Optional[AuditLog]:
    """Append a row to ``audit_log`` describing one tenant-scoped mutation.

    Returns the created row (or None on failure — the caller never has
    to handle the error).

    :param principal: Auth principal — drives ``tenant_id``,
        ``actor_user_id``, ``actor_principal``.
    :param action: Dot-namespaced verb. Example: ``"webhook.created"``.
    :param resource_type: Type slug. Example: ``"webhook"``.
    :param resource_id: ID of the changed row (string).
    :param before: Snapshot of the row before the change. ``None`` for
        creates.
    :param after: Snapshot after the change. ``None`` for deletes.
    :param request: Optional FastAPI request, used to attach
        request_id / IP / user-agent into the metadata column.
    :param extra_meta: Anything else to record in the metadata column.
    """
    try:
        meta: dict[str, Any] = {}
        if request is not None:
            meta["request_id"] = request.headers.get("X-Request-Id")
            meta["user_agent"] = request.headers.get("User-Agent")
            client = getattr(request, "client", None)
            if client is not None and getattr(client, "host", None):
                meta["ip"] = client.host
        if extra_meta:
            meta.update(dict(extra_meta))
        # Drop None values so the JSONB stays compact.
        meta = {k: v for k, v in meta.items() if v is not None}

        row = AuditLog(
            tenant_id=principal.tenant.id,
            actor_user_id=principal.user_id,
            actor_principal=_actor_principal(principal),
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before=_normalize_snapshot(before),
            after=_normalize_snapshot(after),
            meta=meta,
        )
        db.add(row)
        await db.flush()
        return row
    except Exception:
        # An audit-log failure must not break the user-facing request.
        # Log loudly; the business write has already committed by the
        # time we get here.
        logger.warning(
            "audit_log write failed (action=%s resource_type=%s)",
            action,
            resource_type,
            exc_info=True,
        )
        return None


# ── System actor ─────────────────────────────────────────────────────


async def system_audit_log(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    before: Optional[Mapping[str, Any]] = None,
    after: Optional[Mapping[str, Any]] = None,
    extra_meta: Optional[Mapping[str, Any]] = None,
) -> Optional[AuditLog]:
    """Emit an audit row for a system-driven mutation (cron, sweep).

    Used when no user/API-key principal is available — e.g. the trial
    expiry sweep flipping ``subscription_status``. ``actor_principal``
    is ``"system"`` and ``actor_user_id`` is ``NULL``.
    """
    try:
        row = AuditLog(
            tenant_id=tenant_id,
            actor_user_id=None,
            actor_principal="system",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before=_normalize_snapshot(before),
            after=_normalize_snapshot(after),
            meta=dict(extra_meta or {}),
        )
        db.add(row)
        await db.flush()
        return row
    except Exception:
        logger.warning(
            "system_audit_log write failed (action=%s)", action, exc_info=True
        )
        return None


# ── Helpers ──────────────────────────────────────────────────────────


def _actor_principal(principal: AuthPrincipal) -> str:
    if principal.source == "api_key":
        return "api_key"
    return "user"


def _normalize_snapshot(value: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Coerce a snapshot mapping into a JSON-safe dict.

    SQLAlchemy's JSONB will reject sets, datetime objects on some
    dialects, etc. We stringify anything that doesn't look JSON-native
    so the audit write never fails on serialisation.
    """
    if value is None:
        return None
    out: dict[str, Any] = {}
    for k, v in value.items():
        out[str(k)] = _jsonable(v)
    return out


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)
