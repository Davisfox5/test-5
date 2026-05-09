"""Read-through Redis cache for ``Tenant`` config.

Tenant rows hold three JSONB blobs (``features_enabled``, ``tenant_context``,
``branding_config``) that are read on nearly every authenticated request and
in dozens of Celery / orchestrator paths, but mutated rarely (admin actions,
Stripe webhooks). Caching them in Redis with a short TTL eliminates the
per-request DB hit without giving up freshness.

Design mirrors ``services/prompt_variant_service.py``'s pattern:
- best-effort Redis (graceful degradation on connection error)
- short TTL (5 minutes) so rare misses still self-heal
- explicit ``invalidate(tenant_id)`` for write paths
- belt-and-suspenders SQLAlchemy ``after_update`` listener wired in
  :func:`register_invalidation_listener` (called from ``models.py``).

The cached payload is a dict; ``deserialize_tenant`` reconstructs a detached
``Tenant`` ORM object so callers don't need to change their access patterns.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.models import Tenant

logger = logging.getLogger(__name__)


_CACHE_TTL_SECONDS = 300  # 5 min — staleness window for tenant config
_KEY_PREFIX = "tenant:cfg:v1:"


# ── Redis access (best-effort) ────────────────────────────────────────────


def _redis():
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(
            get_settings().REDIS_URL, decode_responses=True
        )
    except Exception:  # pragma: no cover — Redis may be down in tests
        return None


def _key(tenant_id: Any) -> str:
    return f"{_KEY_PREFIX}{tenant_id}"


# ── Serialization helpers ────────────────────────────────────────────────


def _serialize(tenant: Tenant) -> Dict[str, Any]:
    """Pull the read-heavy columns into a dict.

    Only the fields actually consumed by callers are included; relationships
    and write-only columns (e.g. password hashes) are skipped.
    """

    def _val(name: str) -> Any:
        v = getattr(tenant, name, None)
        if isinstance(v, _uuid.UUID):
            return str(v)
        if isinstance(v, datetime):
            return v.isoformat()
        return v

    payload: Dict[str, Any] = {}
    for col in tenant.__table__.columns:  # type: ignore[attr-defined]
        payload[col.name] = _val(col.name)
    return payload


def _deserialize(data: Dict[str, Any]) -> Tenant:
    """Build a detached ``Tenant`` ORM object from a cached payload.

    The returned instance is *not* attached to a session; callers should
    treat it as read-only. Mutations must go through the DB and call
    :func:`invalidate` afterwards.
    """
    kwargs: Dict[str, Any] = {}
    for col in Tenant.__table__.columns:  # type: ignore[attr-defined]
        if col.name not in data:
            continue
        v = data[col.name]
        if v is None:
            kwargs[col.name] = None
            continue
        # Coerce a few well-known types back from JSON-friendly forms.
        if str(col.type).startswith("UUID") and isinstance(v, str):
            try:
                kwargs[col.name] = _uuid.UUID(v)
                continue
            except ValueError:
                pass
        if "DATETIME" in str(col.type).upper() and isinstance(v, str):
            try:
                kwargs[col.name] = datetime.fromisoformat(v)
                continue
            except ValueError:
                pass
        kwargs[col.name] = v
    return Tenant(**kwargs)


# ── Public API ────────────────────────────────────────────────────────────


def cache_get(tenant_id: Any) -> Optional[Tenant]:
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(_key(tenant_id))
        if not raw:
            return None
        return _deserialize(json.loads(raw))
    except Exception:
        logger.debug("tenant_cache get failed (non-fatal)", exc_info=True)
        return None


def cache_set(tenant: Tenant) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.setex(
            _key(tenant.id),
            _CACHE_TTL_SECONDS,
            json.dumps(_serialize(tenant), default=str),
        )
    except Exception:
        logger.debug("tenant_cache set failed (non-fatal)", exc_info=True)


def invalidate(tenant_id: Any) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_key(tenant_id))
    except Exception:
        logger.debug("tenant_cache invalidate failed (non-fatal)", exc_info=True)


async def load_tenant(db: AsyncSession, tenant_id: Any) -> Optional[Tenant]:
    """Async read-through fetch. Returns a cached or freshly-loaded Tenant."""
    cached = cache_get(tenant_id)
    if cached is not None:
        return cached
    tenant = await db.get(Tenant, tenant_id)
    if tenant is not None:
        cache_set(tenant)
    return tenant


def load_tenant_sync(session: Session, tenant_id: Any) -> Optional[Tenant]:
    """Sync read-through fetch — for Celery tasks."""
    cached = cache_get(tenant_id)
    if cached is not None:
        return cached
    tenant = session.get(Tenant, tenant_id)
    if tenant is not None:
        cache_set(tenant)
    return tenant


# ── SQLAlchemy invalidation listener ──────────────────────────────────────


def register_invalidation_listener() -> None:
    """Attach a session-event listener that busts the cache on Tenant writes.

    Idempotent — safe to call multiple times. Wired from :mod:`models`.
    """
    from sqlalchemy import event

    if getattr(register_invalidation_listener, "_attached", False):
        return

    @event.listens_for(Tenant, "after_update", propagate=True)
    def _on_tenant_update(_mapper, _conn, target):  # type: ignore[no-redef]
        try:
            invalidate(target.id)
        except Exception:
            logger.debug("tenant invalidation hook failed", exc_info=True)

    @event.listens_for(Tenant, "after_delete", propagate=True)
    def _on_tenant_delete(_mapper, _conn, target):  # type: ignore[no-redef]
        try:
            invalidate(target.id)
        except Exception:
            logger.debug("tenant invalidation hook failed", exc_info=True)

    register_invalidation_listener._attached = True  # type: ignore[attr-defined]
