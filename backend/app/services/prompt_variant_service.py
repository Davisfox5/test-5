"""Prompt variant routing, hot-load, and rollout management.

Three production AI surfaces — analysis, email_classifier, email_reply — used
to embed their system prompts as Python constants.  This service replaces those
constants with database-backed :class:`PromptVariant` rows that can be:

- Versioned (each new prompt is a new row pointing at its parent)
- A/B-tested (status='shadow' / 'canary' / 'active' with hash-based bucketing)
- Hot-reloaded (Redis-cached lookup with 60s TTL — no restart needed)
- Rolled back (toggle status; cache busts itself within 60s)

A tenant always lands on the same variant within a test window because we hash
``(tenant_id, surface)`` to assign the bucket.  This keeps the user experience
consistent during an A/B test.

If no rows exist for a surface (e.g. on a fresh deploy before
``seed_prompt_variants`` has run) we fall back to the original hard-coded
template that the producer module ships with — see
:func:`get_prompt_template`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid as _uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.models import PromptVariant, Tenant, TenantPromptConfig

logger = logging.getLogger(__name__)


# Bucket allocation (mod 100). Defaults: 5% shadow, 15% canary, 80% active.
SHADOW_PCT = int(os.environ.get("CALLSIGHT_AB_SHADOW_PCT", "5"))
CANARY_PCT = int(os.environ.get("CALLSIGHT_AB_CANARY_PCT", "15"))

# Variant cache TTL — 60s gives "hot reload without restart" feel.
_CACHE_TTL_SECONDS = 60

# In-process L1 cache.  Keyed by (surface, tier, channel, status).
_L1: Dict[Tuple[str, Optional[str], Optional[str], str], Tuple[float, Optional[Dict[str, Any]]]] = {}


# ── Redis L2 cache (best-effort, non-fatal) ──────────────────────────────


def _redis():
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    except Exception:  # pragma: no cover — Redis may be down in tests
        return None


def _cache_key(surface: str, tier: Optional[str], channel: Optional[str], status: str) -> str:
    return f"prompt_variant:{surface}:{tier or '*'}:{channel or '*'}:{status}"


def bust_cache() -> None:
    """Invalidate all in-process and Redis variant caches.

    Call this after promoting / rolling back a variant via the admin tool so
    the new active variant is picked up immediately rather than after the
    60s TTL.
    """
    _L1.clear()
    r = _redis()
    if r is not None:
        try:
            for key in r.scan_iter("prompt_variant:*"):
                r.delete(key)
        except Exception:
            logger.exception("Variant cache bust failed in Redis (non-fatal)")


# ── Variant lookup ───────────────────────────────────────────────────────


def _serialize(variant: Optional[PromptVariant]) -> Optional[Dict[str, Any]]:
    if variant is None:
        return None
    return {
        "id": str(variant.id),
        "name": variant.name,
        "version": variant.version,
        "status": variant.status,
        "target_surface": variant.target_surface,
        "target_tier": variant.target_tier,
        "target_channel": variant.target_channel,
        "prompt_template": variant.prompt_template,
    }


_STATUSES = ("shadow", "canary", "active")


def _select_all_statuses_stmt(
    surface: str, tier: Optional[str], channel: Optional[str]
):
    """Fetch all shadow/canary/active candidates in one round trip.

    The caller buckets by ``.status`` in Python. Ordering matches
    :func:`_select_stmt` so the first row per status is still "most
    specific match wins". Cost: 1 query instead of 3 on a cold cache.
    """
    return (
        select(PromptVariant)
        .where(
            PromptVariant.target_surface == surface,
            PromptVariant.status.in_(_STATUSES),
        )
        .order_by(
            PromptVariant.status,
            (PromptVariant.target_tier == tier).desc(),
            (PromptVariant.target_channel == channel).desc(),
            PromptVariant.created_at.desc(),
        )
    )


def _pick_best_per_status(
    rows: List[PromptVariant],
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Keep the first row (best match) per status. Others are discarded."""
    best: Dict[str, Optional[Dict[str, Any]]] = {s: None for s in _STATUSES}
    for row in rows:
        if best.get(row.status) is None:
            best[row.status] = _serialize(row)
    return best


def _cache_store(
    surface: str,
    tier: Optional[str],
    channel: Optional[str],
    status: str,
    payload: Optional[Dict[str, Any]],
) -> None:
    """Write a serialized variant to both L1 and L2 caches."""
    _L1[(surface, tier, channel, status)] = (
        time.time() + _CACHE_TTL_SECONDS,
        payload,
    )
    r = _redis()
    if r is not None:
        try:
            r.setex(
                _cache_key(surface, tier, channel, status),
                _CACHE_TTL_SECONDS,
                json.dumps(payload) if payload else "null",
            )
        except Exception:
            logger.exception("Variant cache write failed in Redis (non-fatal)")


def _cache_read(
    surface: str, tier: Optional[str], channel: Optional[str], status: str
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Return (hit, payload) — (False, None) when cache misses."""
    cached = _L1.get((surface, tier, channel, status))
    if cached is not None and cached[0] > time.time():
        return True, cached[1]
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_cache_key(surface, tier, channel, status))
            if raw is not None:
                payload = json.loads(raw) if raw != "null" else None
                _L1[(surface, tier, channel, status)] = (
                    time.time() + _CACHE_TTL_SECONDS,
                    payload,
                )
                return True, payload
        except Exception:
            logger.exception("Variant cache read failed in Redis (non-fatal)")
    return False, None


def _select_stmt(surface: str, tier: Optional[str], channel: Optional[str], status: str):
    """Most specific match wins: tier+channel match > tier match > generic."""
    return (
        select(PromptVariant)
        .where(
            PromptVariant.target_surface == surface,
            PromptVariant.status == status,
        )
        .order_by(
            # Prefer rows that match the tier first, then those that match the
            # channel, then the most recent.
            (PromptVariant.target_tier == tier).desc(),
            (PromptVariant.target_channel == channel).desc(),
            PromptVariant.created_at.desc(),
        )
        .limit(1)
    )


async def _load_variant_async(
    db: AsyncSession,
    surface: str,
    tier: Optional[str],
    channel: Optional[str],
    status: str,
) -> Optional[Dict[str, Any]]:
    key = _cache_key(surface, tier, channel, status)

    # L1 (in-process)
    cached = _L1.get((surface, tier, channel, status))
    if cached is not None and cached[0] > time.time():
        return cached[1]

    # L2 (Redis)
    r = _redis()
    if r is not None:
        try:
            raw = r.get(key)
            if raw:
                payload = json.loads(raw)
                _L1[(surface, tier, channel, status)] = (time.time() + _CACHE_TTL_SECONDS, payload)
                return payload
        except Exception:
            logger.exception("Variant cache read failed in Redis (non-fatal)")

    # DB
    row = (await db.execute(_select_stmt(surface, tier, channel, status))).scalar_one_or_none()
    payload = _serialize(row)

    _L1[(surface, tier, channel, status)] = (time.time() + _CACHE_TTL_SECONDS, payload)
    if r is not None:
        try:
            r.setex(key, _CACHE_TTL_SECONDS, json.dumps(payload) if payload else "null")
        except Exception:
            logger.exception("Variant cache write failed in Redis (non-fatal)")
    return payload


def _load_variant_sync(
    db: Session,
    surface: str,
    tier: Optional[str],
    channel: Optional[str],
    status: str,
) -> Optional[Dict[str, Any]]:
    """Sync variant for Celery worker contexts."""
    cached = _L1.get((surface, tier, channel, status))
    if cached is not None and cached[0] > time.time():
        return cached[1]

    key = _cache_key(surface, tier, channel, status)
    r = _redis()
    if r is not None:
        try:
            raw = r.get(key)
            if raw:
                payload = json.loads(raw) if raw != "null" else None
                _L1[(surface, tier, channel, status)] = (time.time() + _CACHE_TTL_SECONDS, payload)
                return payload
        except Exception:
            logger.exception("Variant cache read failed in Redis (non-fatal)")

    row = db.execute(_select_stmt(surface, tier, channel, status)).scalar_one_or_none()
    payload = _serialize(row)
    _L1[(surface, tier, channel, status)] = (time.time() + _CACHE_TTL_SECONDS, payload)
    if r is not None:
        try:
            r.setex(key, _CACHE_TTL_SECONDS, json.dumps(payload) if payload else "null")
        except Exception:
            logger.exception("Variant cache write failed in Redis (non-fatal)")
    return payload


# ── Batched three-status loader ──────────────────────────────────────────


async def _load_all_variants_async(
    db: AsyncSession,
    surface: str,
    tier: Optional[str],
    channel: Optional[str],
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Return the best shadow/canary/active variant per status, in one query.

    Cache-fills from L1 + L2 first; only falls through to DB when at least
    one status has no cache entry.
    """
    result: Dict[str, Optional[Dict[str, Any]]] = {}
    missing: List[str] = []
    for status in _STATUSES:
        hit, payload = _cache_read(surface, tier, channel, status)
        if hit:
            result[status] = payload
        else:
            missing.append(status)
    if not missing:
        return result

    rows = (
        await db.execute(_select_all_statuses_stmt(surface, tier, channel))
    ).scalars().all()
    fresh = _pick_best_per_status(rows)
    for status in _STATUSES:
        if status in missing:
            result[status] = fresh[status]
            _cache_store(surface, tier, channel, status, fresh[status])
    return result


def _load_all_variants_sync(
    db: Session,
    surface: str,
    tier: Optional[str],
    channel: Optional[str],
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Sync analogue of :func:`_load_all_variants_async`."""
    result: Dict[str, Optional[Dict[str, Any]]] = {}
    missing: List[str] = []
    for status in _STATUSES:
        hit, payload = _cache_read(surface, tier, channel, status)
        if hit:
            result[status] = payload
        else:
            missing.append(status)
    if not missing:
        return result

    rows = list(
        db.execute(_select_all_statuses_stmt(surface, tier, channel)).scalars().all()
    )
    fresh = _pick_best_per_status(rows)
    for status in _STATUSES:
        if status in missing:
            result[status] = fresh[status]
            _cache_store(surface, tier, channel, status, fresh[status])
    return result


# ── Routing (A/B bucket) ─────────────────────────────────────────────────


@dataclass
class VariantSelection:
    variant_id: Optional[str]
    prompt_template: Optional[str]
    name: str
    status: str  # 'shadow' | 'canary' | 'active' | 'fallback'


def _bucket(tenant_id: Any, surface: str) -> int:
    h = hashlib.sha256(f"{tenant_id}:{surface}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 100


def _maybe_pin(
    tenant: Optional[Tenant], surface: str, all_variants: Dict[str, Optional[Dict[str, Any]]]
) -> Optional[Dict[str, Any]]:
    """Honour an explicit per-tenant pin in ``TenantPromptConfig.active_prompt_variant_ids``.

    Lets an enterprise tenant lock to a specific variant — overrides the bucket.
    """
    if tenant is None:
        return None
    config = getattr(tenant, "prompt_config", None)
    if config is None:
        return None
    pinned = (config.active_prompt_variant_ids or {}).get(surface)
    if not pinned:
        return None
    # Find the pinned variant among the loaded ones; if not present we silently
    # fall through to bucket selection rather than failing the request.
    for v in all_variants.values():
        if v and v.get("id") == pinned:
            return v
    return None


async def select_variant_async(
    db: AsyncSession,
    tenant: Tenant,
    surface: str,
    tier: Optional[str] = None,
    channel: Optional[str] = None,
    fallback_template: Optional[str] = None,
) -> VariantSelection:
    """Resolve the prompt variant for this tenant + surface.

    Bucket logic:
    - hash(tenant_id, surface) mod 100
    - [0, SHADOW_PCT)         → shadow
    - [SHADOW_PCT, +CANARY)   → canary
    - else                    → active
    Falls through to ``fallback_template`` if no row exists for any status.
    """
    variants = await _load_all_variants_async(db, surface, tier, channel)

    pinned = _maybe_pin(tenant, surface, variants)
    if pinned is not None:
        return VariantSelection(
            variant_id=pinned["id"],
            prompt_template=pinned["prompt_template"],
            name=pinned.get("name", "pinned"),
            status=pinned.get("status", "active"),
        )

    bucket = _bucket(tenant.id, surface)
    chosen: Optional[Dict[str, Any]] = None
    chosen_status = "fallback"
    if bucket < SHADOW_PCT and variants["shadow"]:
        chosen, chosen_status = variants["shadow"], "shadow"
    elif bucket < SHADOW_PCT + CANARY_PCT and variants["canary"]:
        chosen, chosen_status = variants["canary"], "canary"
    elif variants["active"]:
        chosen, chosen_status = variants["active"], "active"

    if chosen is None:
        # No DB rows yet — use the fallback template the producer ships with.
        return VariantSelection(
            variant_id=None,
            prompt_template=fallback_template,
            name="fallback",
            status="fallback",
        )

    return VariantSelection(
        variant_id=chosen["id"],
        prompt_template=chosen["prompt_template"],
        name=chosen.get("name", chosen_status),
        status=chosen_status,
    )


def select_variant_sync(
    db: Session,
    tenant: Tenant,
    surface: str,
    tier: Optional[str] = None,
    channel: Optional[str] = None,
    fallback_template: Optional[str] = None,
) -> VariantSelection:
    """Sync analogue of :func:`select_variant_async` for Celery workers."""
    variants = _load_all_variants_sync(db, surface, tier, channel)

    pinned = _maybe_pin(tenant, surface, variants)
    if pinned is not None:
        return VariantSelection(
            variant_id=pinned["id"],
            prompt_template=pinned["prompt_template"],
            name=pinned.get("name", "pinned"),
            status=pinned.get("status", "active"),
        )

    bucket = _bucket(tenant.id, surface)
    chosen: Optional[Dict[str, Any]] = None
    chosen_status = "fallback"
    if bucket < SHADOW_PCT and variants["shadow"]:
        chosen, chosen_status = variants["shadow"], "shadow"
    elif bucket < SHADOW_PCT + CANARY_PCT and variants["canary"]:
        chosen, chosen_status = variants["canary"], "canary"
    elif variants["active"]:
        chosen, chosen_status = variants["active"], "active"

    if chosen is None:
        return VariantSelection(
            variant_id=None,
            prompt_template=fallback_template,
            name="fallback",
            status="fallback",
        )

    return VariantSelection(
        variant_id=chosen["id"],
        prompt_template=chosen["prompt_template"],
        name=chosen.get("name", chosen_status),
        status=chosen_status,
    )


# ── Convenience: resolve UUID for the variant_id column ──────────────────


def to_uuid(maybe_id: Optional[str]) -> Optional[_uuid.UUID]:
    if not maybe_id:
        return None
    try:
        return _uuid.UUID(maybe_id)
    except (TypeError, ValueError):
        return None


# ── Seeding helper (idempotent) ──────────────────────────────────────────


def seed_default_variants(db: Session, surface_to_template: Dict[str, str]) -> Dict[str, str]:
    """Seed the original hard-coded prompts as version=1 active variants.

    Idempotent — re-running this on an already-seeded DB is a no-op.  The
    intended caller is a one-shot script during deploy.

    Returns mapping of surface → variant id (string).  Empty dict if any
    surface already had an active variant.
    """
    created: Dict[str, str] = {}
    for surface, template in surface_to_template.items():
        existing = db.execute(
            select(PromptVariant).where(
                PromptVariant.target_surface == surface,
                PromptVariant.status == "active",
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        variant = PromptVariant(
            name=f"{surface}-v1-baseline",
            description=f"Initial baseline prompt for {surface} surface (seeded from code).",
            prompt_template=template,
            target_surface=surface,
            version=1,
            status="active",
        )
        db.add(variant)
        db.flush()
        created[surface] = str(variant.id)
    db.commit()
    bust_cache()
    return created
