"""Debounce scheduler for LINDA context rebuilds.

When a KB doc is created/updated/deleted we want to rebuild the tenant's
tenant_context brief — but not *per doc*. Bulk uploads would otherwise fire
dozens of rebuilds. We coalesce them into a single rebuild that runs 30s
after the last trigger.

Implementation is a cooperative Redis-backed timer:

* Each call increments ``pending:{tenant_id}`` so we know a rebuild is needed.
* Each call also rewrites ``debounce:{tenant_id}`` with a new TTL. The Celery
  task checks whether the timer has elapsed; if someone pushed the TTL forward
  while it was sleeping, it reschedules itself and exits.

Costs nothing beyond the Redis we already run.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

import redis.asyncio as aioredis

from backend.app.config import get_settings

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 30
_PENDING_KEY = "ctx_rebuild:pending:{tenant_id}"
_DEBOUNCE_KEY = "ctx_rebuild:debounce:{tenant_id}"


def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)


async def schedule_context_rebuild(
    tenant_id: uuid.UUID,
    *,
    full: bool = False,
) -> None:
    """Mark the tenant for a rebuild and enqueue a debounced Celery task.

    Safe to call on every KB mutation. Dropping the call (e.g., Redis down)
    is not catastrophic — the nightly scheduled rebuild will catch up.
    """
    try:
        r = _get_redis()
        now_ts = time.time()
        pending_key = _PENDING_KEY.format(tenant_id=tenant_id)
        debounce_key = _DEBOUNCE_KEY.format(tenant_id=tenant_id)

        await r.set(pending_key, "1", ex=3600)
        await r.set(debounce_key, str(now_ts + _DEBOUNCE_SECONDS), ex=_DEBOUNCE_SECONDS * 4)
        await r.aclose()
    except Exception:
        logger.debug("Failed to mark pending context rebuild", exc_info=True)
        return

    # Enqueue the actual rebuild with a countdown so a rapid flurry of uploads
    # collapses into one run. Celery deduplicates by virtue of our debounce
    # check below refusing to run if the token has been pushed forward.
    try:
        from backend.app.tasks import rebuild_tenant_context

        rebuild_tenant_context.apply_async(
            args=[str(tenant_id), full],
            countdown=_DEBOUNCE_SECONDS,
        )
    except Exception:
        # If Celery is unavailable in this process (e.g., during tests), the
        # scheduling is best-effort and the admin rebuild endpoint is the
        # fallback.
        logger.debug("Failed to enqueue context rebuild task", exc_info=True)


_CUSTOMER_DEBOUNCE_SECONDS = 30
_CUSTOMER_DEBOUNCE_KEY = "customer_brief:debounce:{customer_id}"
_CUSTOMER_PENDING_KEY = "customer_brief:pending:{customer_id}"


async def schedule_customer_brief_rebuild(
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
) -> None:
    """Mark a customer for a brief rebuild, debounced the same way as the
    tenant brief. Safe to call on interaction close or any CRM update."""
    try:
        r = _get_redis()
        now_ts = time.time()
        await r.set(
            _CUSTOMER_PENDING_KEY.format(customer_id=customer_id),
            str(tenant_id),
            ex=3600,
        )
        await r.set(
            _CUSTOMER_DEBOUNCE_KEY.format(customer_id=customer_id),
            str(now_ts + _CUSTOMER_DEBOUNCE_SECONDS),
            ex=_CUSTOMER_DEBOUNCE_SECONDS * 4,
        )
        await r.aclose()
    except Exception:
        logger.debug("Failed to mark pending customer brief rebuild", exc_info=True)
        return

    try:
        from backend.app.tasks import rebuild_customer_brief

        rebuild_customer_brief.apply_async(
            args=[str(tenant_id), str(customer_id)],
            countdown=_CUSTOMER_DEBOUNCE_SECONDS,
        )
    except Exception:
        logger.debug("Failed to enqueue customer brief rebuild task", exc_info=True)


async def claim_customer_debounce(customer_id: uuid.UUID) -> bool:
    """Mirror of ``claim_debounce`` for customer briefs."""
    try:
        r = _get_redis()
        key = _CUSTOMER_DEBOUNCE_KEY.format(customer_id=customer_id)
        raw = await r.get(key)
        if raw is None:
            await r.aclose()
            return False
        if time.time() < float(raw):
            await r.aclose()
            return False
        await r.delete(
            key,
            _CUSTOMER_PENDING_KEY.format(customer_id=customer_id),
        )
        await r.aclose()
        return True
    except Exception:
        logger.debug("claim_customer_debounce errored — assuming run", exc_info=True)
        return True


async def claim_debounce(tenant_id: uuid.UUID) -> bool:
    """Called from inside the Celery task body to decide whether to run now.

    Returns True when the debounce window has actually elapsed (we're the
    winner). Returns False when someone bumped the key forward while we were
    asleep; the caller should exit without running, because a fresh task is
    already scheduled.
    """
    try:
        r = _get_redis()
        debounce_key = _DEBOUNCE_KEY.format(tenant_id=tenant_id)
        raw = await r.get(debounce_key)
        if raw is None:
            # No pending timer — someone else claimed it already.
            await r.aclose()
            return False
        target_ts = float(raw)
        if time.time() < target_ts:
            await r.aclose()
            return False
        await r.delete(
            debounce_key,
            _PENDING_KEY.format(tenant_id=tenant_id),
        )
        await r.aclose()
        return True
    except Exception:
        logger.debug("claim_debounce errored — assuming we should run", exc_info=True)
        return True
