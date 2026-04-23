"""Dev-only vector-store health tracking.

Records per-query latency to Redis in rolling hourly buckets so the admin
endpoint and the daily threshold check can compute p95 without needing a
dedicated metrics system. Costs nothing beyond the Redis we already run.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Dict, Iterable, List, Optional

import redis.asyncio as aioredis

from backend.app.config import get_settings

logger = logging.getLogger(__name__)

_LATENCY_KEY_PREFIX = "vector_health:lat"  # f"{prefix}:{yyyymmddhh}"
_MILESTONE_KEY = "vector_health:milestones"  # hash: milestone -> "1" once alerted
_ALERT_STATE_KEY = "vector_health:alert"  # hash with streak counters
_BUCKET_TTL_SECONDS = 60 * 60 * 24 * 14  # keep 14 days of history

_ALERT_TAG = "[VECTOR_HEALTH_ALERT]"


def _bucket_key(ts: Optional[float] = None) -> str:
    ts = ts if ts is not None else time.time()
    tm = time.gmtime(ts)
    return f"{_LATENCY_KEY_PREFIX}:{tm.tm_year:04d}{tm.tm_mon:02d}{tm.tm_mday:02d}{tm.tm_hour:02d}"


def _get_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def record_search_latency(
    tenant_id: uuid.UUID,
    elapsed_ms: float,
) -> None:
    """Append a single search latency sample to the current hourly bucket."""
    try:
        r = _get_redis()
        key = _bucket_key()
        await r.rpush(key, f"{elapsed_ms:.2f}")
        await r.expire(key, _BUCKET_TTL_SECONDS)
        await r.aclose()
    except Exception:
        # Never fail a search because monitoring hiccuped.
        logger.debug("Failed to record vector search latency", exc_info=True)


async def _collect_samples(hours: int = 24) -> List[float]:
    r = _get_redis()
    try:
        now = time.time()
        keys = [_bucket_key(now - i * 3600) for i in range(hours)]
        samples: List[float] = []
        for k in keys:
            values = await r.lrange(k, 0, -1)
            for v in values:
                try:
                    samples.append(float(v))
                except ValueError:
                    continue
        return samples
    finally:
        await r.aclose()


def _percentile(values: Iterable[float], p: float) -> float:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return 0.0
    if p <= 0:
        return xs[0]
    if p >= 100:
        return xs[-1]
    k = (len(xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


async def current_metrics(total_chunks: Optional[int] = None) -> Dict[str, float]:
    """Return the latency percentiles over the last 24h plus optional count."""
    samples = await _collect_samples(24)
    return {
        "samples_24h": len(samples),
        "p50_ms": _percentile(samples, 50),
        "p95_ms": _percentile(samples, 95),
        "p99_ms": _percentile(samples, 99),
        "total_chunks": total_chunks if total_chunks is not None else -1,
    }


async def streak_days() -> int:
    """How many consecutive days the p95 has been over the threshold."""
    r = _get_redis()
    try:
        raw = await r.hget(_ALERT_STATE_KEY, "p95_streak_days")
        return int(raw) if raw else 0
    finally:
        await r.aclose()


async def update_streak(p95_ms: float, threshold_ms: int) -> int:
    """Update the running streak counter once per day. Returns current streak."""
    r = _get_redis()
    try:
        today = time.strftime("%Y%m%d", time.gmtime())
        last = await r.hget(_ALERT_STATE_KEY, "last_check_day")
        if last == today:
            return int(await r.hget(_ALERT_STATE_KEY, "p95_streak_days") or 0)

        streak = int(await r.hget(_ALERT_STATE_KEY, "p95_streak_days") or 0)
        if p95_ms >= threshold_ms:
            streak += 1
        else:
            streak = 0
        await r.hset(
            _ALERT_STATE_KEY,
            mapping={"p95_streak_days": str(streak), "last_check_day": today},
        )
        return streak
    finally:
        await r.aclose()


async def milestone_already_alerted(size: int) -> bool:
    r = _get_redis()
    try:
        return bool(await r.hget(_MILESTONE_KEY, str(size)))
    finally:
        await r.aclose()


async def mark_milestone_alerted(size: int) -> None:
    r = _get_redis()
    try:
        await r.hset(_MILESTONE_KEY, str(size), "1")
    finally:
        await r.aclose()


def alert_log(message: str) -> None:
    """Emit a distinctive WARN log so dev tooling can grep for it."""
    logger.warning("%s %s", _ALERT_TAG, message)
