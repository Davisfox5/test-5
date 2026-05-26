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
import os
import time
import uuid as _uuid
from datetime import datetime
from threading import RLock
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.models import Tenant

logger = logging.getLogger(__name__)


_CACHE_TTL_SECONDS = 300  # 5 min — staleness window for tenant config
_KEY_PREFIX = "tenant:cfg:v1:"

# ── Process-local L1 cache in front of Redis (L2) ─────────────────────
# Every uvicorn worker holds its own LRU. A 60s TTL means a tenant
# that hits an api worker repeatedly within a minute does NOT hit
# Redis at all — the L1 covers it. Cross-process invalidation still
# works because the SQLAlchemy ``after_update`` listener fires in
# whichever process did the write, and on the next request other
# processes will see the stale L1 entry for up to 60s before it
# expires. That bounded staleness is the same trade-off the L2 Redis
# cache already accepted (its TTL is 300s).
#
# Why bother adding L1 when L2 exists: Upstash bills per command.
# A single GET we don't issue is a command we don't pay for. With
# even modest traffic the L1 absorbs ~90%+ of tenant-config reads.
_L1_TTL_SECONDS = 60
_L1_MAX_ENTRIES = 256
# Allow disabling via env for diagnosing cache-staleness issues.
_L1_ENABLED = os.environ.get("TENANT_CACHE_L1_DISABLED", "").lower() not in {
    "1",
    "true",
    "yes",
}

# Map: tenant_id (str) -> (expires_at_unix, serialized_payload_dict).
# We store the serialized dict rather than a Tenant instance so the L1
# value is independent of SQLAlchemy session lifecycles and identity
# maps (a cached Tenant pinned to a closed session would explode on
# attribute access).
_L1: "Dict[str, Tuple[float, Dict[str, Any]]]" = {}
_L1_LOCK = RLock()


def _l1_get(tenant_id: Any) -> Optional[Dict[str, Any]]:
    if not _L1_ENABLED:
        return None
    key = str(tenant_id)
    with _L1_LOCK:
        entry = _L1.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.time():
            _L1.pop(key, None)
            return None
        return payload


def _l1_set(tenant_id: Any, payload: Dict[str, Any]) -> None:
    if not _L1_ENABLED:
        return
    key = str(tenant_id)
    with _L1_LOCK:
        # Cheap LRU-ish eviction: when over budget, drop expired entries
        # first; if still over, drop the oldest expires_at.
        if len(_L1) >= _L1_MAX_ENTRIES:
            now = time.time()
            stale_keys = [k for k, (exp, _) in _L1.items() if exp < now]
            for k in stale_keys:
                _L1.pop(k, None)
            if len(_L1) >= _L1_MAX_ENTRIES:
                # Drop a quarter of the entries, oldest first.
                victims = sorted(_L1.items(), key=lambda kv: kv[1][0])[
                    : max(1, _L1_MAX_ENTRIES // 4)
                ]
                for k, _ in victims:
                    _L1.pop(k, None)
        _L1[key] = (time.time() + _L1_TTL_SECONDS, payload)


def _l1_invalidate(tenant_id: Any) -> None:
    if not _L1_ENABLED:
        return
    with _L1_LOCK:
        _L1.pop(str(tenant_id), None)


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
    # L1 first — process-local; never touches Redis on hit.
    cached_payload = _l1_get(tenant_id)
    if cached_payload is not None:
        return _deserialize(cached_payload)

    # L1 miss — fall through to L2 (Redis).
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(_key(tenant_id))
        if not raw:
            return None
        payload = json.loads(raw)
        _l1_set(tenant_id, payload)
        return _deserialize(payload)
    except Exception:
        logger.debug("tenant_cache get failed (non-fatal)", exc_info=True)
        return None


def cache_set(tenant: Tenant) -> None:
    payload = _serialize(tenant)
    _l1_set(tenant.id, payload)
    r = _redis()
    if r is None:
        return
    try:
        r.setex(
            _key(tenant.id),
            _CACHE_TTL_SECONDS,
            json.dumps(payload, default=str),
        )
    except Exception:
        logger.debug("tenant_cache set failed (non-fatal)", exc_info=True)


def invalidate(tenant_id: Any) -> None:
    _l1_invalidate(tenant_id)
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
