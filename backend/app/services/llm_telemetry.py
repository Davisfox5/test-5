"""Adaptive ``max_tokens`` ceiling — telemetry + learned ceilings.

Anthropic bills per generated output token, not per ``max_tokens`` cap, so
this isn't primarily a cost lever. It is a *quality and observability*
tool that replaces guess-and-check ceilings with measured ones:

* Records every completion's usage stats to ``llm_call_telemetry``.
* A nightly Celery task aggregates into ``llm_ceiling_recommendation`` —
  one row per (call_site, tier) holding p50/p95/p99 + a recommended
  ceiling sized at ``p99 * 1.2`` (or ``max_observed * 1.5`` if recent
  calls truncated > 5%).
* ``learned_ceiling(call_site, tier)`` returns that ceiling, cached
  in-process for an hour. ``compute_max_tokens`` consults it before
  falling back to the static per-tier ceiling.

Recording is fire-and-forget — a write failure must never fail a customer
LLM call, so all paths swallow exceptions and log only.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


# ── In-process learned-ceiling cache ──────────────────────────────────────
#
# The recommendation table is recomputed once per day, so a 1-hour cache
# is fine — readers will pick up new recommendations the next morning.

_CACHE_TTL_SECONDS = 3600
_cache: Dict[Tuple[str, str], Tuple[float, Optional[int]]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: Tuple[str, str]) -> Tuple[bool, Optional[int]]:
    """Return ``(hit, value)`` for an in-process cached learned ceiling."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return False, None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            _cache.pop(key, None)
            return False, None
        return True, value


def _cache_put(key: Tuple[str, str], value: Optional[int]) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)


def invalidate_cache() -> None:
    """Drop all cached recommendations. Called from the daily recompute task."""
    with _cache_lock:
        _cache.clear()


# ── Recording ──────────────────────────────────────────────────────────────


def _extract_usage(response: Any) -> Dict[str, Any]:
    """Pull token counts + stop_reason out of an Anthropic response object.

    Tolerant of dict, dataclass-like, or SDK object shapes; missing fields
    default to 0 / None so a partial response never breaks recording.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason is None and isinstance(response, dict):
        stop_reason = response.get("stop_reason")
    model = getattr(response, "model", None)
    if model is None and isinstance(response, dict):
        model = response.get("model")

    def _u(field: str) -> int:
        if usage is None:
            return 0
        if isinstance(usage, dict):
            return int(usage.get(field, 0) or 0)
        return int(getattr(usage, field, 0) or 0)

    return {
        "input_tokens": _u("input_tokens"),
        "output_tokens": _u("output_tokens"),
        "cache_read_input_tokens": _u("cache_read_input_tokens"),
        "cache_creation_input_tokens": _u("cache_creation_input_tokens"),
        "stop_reason": stop_reason,
        "model": model,
    }


_INSERT_SQL = text(
    """
    INSERT INTO llm_call_telemetry (
        call_site, tier, model, request_max_tokens,
        input_tokens, output_tokens,
        cache_read_input_tokens, cache_creation_input_tokens,
        stop_reason, truncated, tenant_id
    ) VALUES (
        :call_site, :tier, :model, :request_max_tokens,
        :input_tokens, :output_tokens,
        :cache_read_input_tokens, :cache_creation_input_tokens,
        :stop_reason, :truncated, :tenant_id
    )
    """
)


def _record_sync(payload: Dict[str, Any]) -> None:
    """Synchronous insert via the Celery-task ``_sync_engine``.

    Pulled lazily to avoid a top-level import cycle with ``backend.app.tasks``.
    """
    try:  # pragma: no cover — defensive
        from backend.app.tasks import _sync_engine  # type: ignore

        with _sync_engine.begin() as conn:
            conn.execute(_INSERT_SQL, payload)
    except Exception:
        logger.debug("llm_telemetry: sync insert failed", exc_info=True)


def record_llm_completion(
    call_site: str,
    tier: str,
    request_max_tokens: int,
    response: Any,
    *,
    tenant_id: Optional[uuid.UUID] = None,
) -> None:
    """Fire-and-forget: record one Anthropic completion's usage stats.

    Safe from any context (async loop, Celery sync task, plain script).
    Errors are swallowed.

    Args:
        call_site: short stable identifier — e.g. ``"entity_resolution"``,
            ``"scorecard_single"``. Used as the aggregation key.
        tier: ``"haiku" | "sonnet" | "opus"``.
        request_max_tokens: the ``max_tokens`` the caller passed in.
        response: the Anthropic SDK response object (sync or async).
        tenant_id: optional, for per-tenant slicing later.
    """
    try:
        usage = _extract_usage(response)
        payload = {
            "call_site": call_site,
            "tier": (tier or "sonnet").lower(),
            "model": usage["model"],
            "request_max_tokens": int(request_max_tokens),
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cache_read_input_tokens": usage["cache_read_input_tokens"],
            "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
            "stop_reason": usage["stop_reason"],
            "truncated": usage["stop_reason"] == "max_tokens",
            "tenant_id": tenant_id,
        }
    except Exception:
        logger.debug("llm_telemetry: payload assembly failed", exc_info=True)
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # In an async context — push the blocking insert to a worker thread
        # so the LLM caller doesn't pay DB latency on the hot path.
        loop.run_in_executor(None, _record_sync, payload)
    else:
        _record_sync(payload)


# ── Learned ceiling lookup ─────────────────────────────────────────────────


_SELECT_RECOMMENDATION_SQL = text(
    """
    SELECT recommended_ceiling
    FROM llm_ceiling_recommendation
    WHERE call_site = :call_site AND tier = :tier
    """
)


def learned_ceiling(call_site: str, tier: str) -> Optional[int]:
    """Return the recommended ``max_tokens`` for this (call_site, tier),
    or ``None`` if no recommendation has been computed yet.

    Cached in-process for 1 hour. Cache is invalidated whenever the
    nightly aggregation task rewrites the recommendation table.
    """
    tier_key = (tier or "sonnet").lower()
    key = (call_site, tier_key)
    hit, cached = _cache_get(key)
    if hit:
        return cached

    try:
        from backend.app.tasks import _sync_engine  # type: ignore

        with _sync_engine.connect() as conn:
            row = conn.execute(
                _SELECT_RECOMMENDATION_SQL,
                {"call_site": call_site, "tier": tier_key},
            ).fetchone()
        value: Optional[int] = int(row[0]) if row else None
    except SQLAlchemyError:
        logger.debug("llm_telemetry: ceiling lookup failed", exc_info=True)
        value = None
    except Exception:
        logger.debug("llm_telemetry: ceiling lookup exception", exc_info=True)
        value = None

    _cache_put(key, value)
    return value


# ── Recompute (called from the daily Celery task) ──────────────────────────


_MIN_SAMPLES = 200
_MIN_AGE_DAYS = 14
_WINDOW_DAYS = 14
_TRUNCATION_THRESHOLD = 0.05  # > 5% truncation → enlarge the ceiling
_HEADROOM_FACTOR = 1.2
_TRUNCATION_HEADROOM_FACTOR = 1.5
_TELEMETRY_TTL_DAYS = 30


_AGGREGATE_SQL = text(
    f"""
    WITH window_calls AS (
        SELECT call_site, tier, output_tokens, truncated
        FROM llm_call_telemetry
        WHERE created_at >= NOW() - INTERVAL '{_WINDOW_DAYS} days'
    ),
    stats AS (
        SELECT
            call_site,
            tier,
            COUNT(*)                                                AS sample_count,
            CAST(percentile_cont(0.50) WITHIN GROUP (ORDER BY output_tokens) AS INTEGER) AS p50,
            CAST(percentile_cont(0.95) WITHIN GROUP (ORDER BY output_tokens) AS INTEGER) AS p95,
            CAST(percentile_cont(0.99) WITHIN GROUP (ORDER BY output_tokens) AS INTEGER) AS p99,
            MAX(output_tokens)                                      AS max_observed,
            AVG(CASE WHEN truncated THEN 1.0 ELSE 0.0 END)::float   AS truncation_rate
        FROM window_calls
        GROUP BY call_site, tier
    ),
    age AS (
        SELECT
            call_site, tier,
            MIN(created_at) AS first_seen_at
        FROM llm_call_telemetry
        GROUP BY call_site, tier
    )
    SELECT
        s.call_site, s.tier, s.sample_count,
        s.p50, s.p95, s.p99, s.max_observed, s.truncation_rate,
        a.first_seen_at
    FROM stats s
    JOIN age a USING (call_site, tier)
    """
)


_UPSERT_RECOMMENDATION_SQL = text(
    """
    INSERT INTO llm_ceiling_recommendation (
        call_site, tier, sample_count, p50, p95, p99,
        max_observed, truncation_rate, recommended_ceiling,
        window_start, window_end, computed_at
    ) VALUES (
        :call_site, :tier, :sample_count, :p50, :p95, :p99,
        :max_observed, :truncation_rate, :recommended_ceiling,
        :window_start, :window_end, NOW()
    )
    ON CONFLICT (call_site, tier) DO UPDATE SET
        sample_count        = EXCLUDED.sample_count,
        p50                 = EXCLUDED.p50,
        p95                 = EXCLUDED.p95,
        p99                 = EXCLUDED.p99,
        max_observed        = EXCLUDED.max_observed,
        truncation_rate     = EXCLUDED.truncation_rate,
        recommended_ceiling = EXCLUDED.recommended_ceiling,
        window_start        = EXCLUDED.window_start,
        window_end          = EXCLUDED.window_end,
        computed_at         = NOW()
    """
)


_TTL_SQL = text(
    f"""
    DELETE FROM llm_call_telemetry
    WHERE created_at < NOW() - INTERVAL '{_TELEMETRY_TTL_DAYS} days'
    """
)


def recompute_ceilings() -> Dict[str, Any]:
    """Recompute per-(call_site, tier) recommendations from the rolling
    14-day window. Skips groups that don't yet have ≥200 samples AND ≥14
    days of history (whichever satisfied first).

    Returns a summary dict suitable for logging from a Celery task.
    """
    from backend.app.tasks import _sync_engine  # type: ignore

    updated = 0
    skipped = 0
    deleted_old = 0
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=_WINDOW_DAYS)

    try:
        with _sync_engine.begin() as conn:
            rows = conn.execute(_AGGREGATE_SQL).fetchall()
            for row in rows:
                (
                    call_site,
                    tier,
                    sample_count,
                    p50,
                    p95,
                    p99,
                    max_observed,
                    truncation_rate,
                    first_seen_at,
                ) = row
                age_days = (
                    (now - first_seen_at).total_seconds() / 86400.0
                    if first_seen_at is not None
                    else 0.0
                )
                if sample_count < _MIN_SAMPLES and age_days < _MIN_AGE_DAYS:
                    skipped += 1
                    continue
                if truncation_rate > _TRUNCATION_THRESHOLD:
                    ceiling = int(max_observed * _TRUNCATION_HEADROOM_FACTOR)
                else:
                    ceiling = int(p99 * _HEADROOM_FACTOR)
                ceiling = max(ceiling, 256)
                conn.execute(
                    _UPSERT_RECOMMENDATION_SQL,
                    {
                        "call_site": call_site,
                        "tier": tier,
                        "sample_count": int(sample_count),
                        "p50": int(p50 or 0),
                        "p95": int(p95 or 0),
                        "p99": int(p99 or 0),
                        "max_observed": int(max_observed or 0),
                        "truncation_rate": float(truncation_rate or 0.0),
                        "recommended_ceiling": ceiling,
                        "window_start": window_start,
                        "window_end": now,
                    },
                )
                updated += 1

            result = conn.execute(_TTL_SQL)
            deleted_old = int(result.rowcount or 0)
    except Exception:
        logger.exception("llm_telemetry: recompute_ceilings failed")
        return {"updated": updated, "skipped": skipped, "error": True}

    invalidate_cache()
    summary = {
        "updated": updated,
        "skipped": skipped,
        "deleted_old": deleted_old,
        "window_days": _WINDOW_DAYS,
    }
    logger.info("llm_telemetry recompute_ceilings: %s", summary)
    return summary
