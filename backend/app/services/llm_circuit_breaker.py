"""Shared circuit breaker for Anthropic credit-balance exhaustion.

An exhausted credit balance is an EXPECTED operational state here (the
balance is kept low deliberately at times), not an incident. Without a
breaker, every poll cycle on every worker slams the API, gets the same
400 back, and ships one Sentry event per attempt (thousands/day across
``email_ingest_poll`` + ``manager_recommendations_build``).

Behavior:

- The first credit-balance 400 **opens** the breaker. State lives in
  Redis (shared by the API, every Celery worker, and beat; survives
  restarts) with an in-process fallback when Redis is unreachable.
- While open, every LLM call raises :class:`LLMCallsSuspended` *before*
  touching the API. Periodic jobs treat that as "defer quietly to the
  next tick" — the Celery base task converts it into a skipped result,
  and the pollers rollback so no cursor advances past unclassified work.
- Every ``LLM_BREAKER_PROBE_INTERVAL_SECONDS`` one caller (fleet-wide,
  via a Redis NX lock) is allowed a 1-token Haiku probe. Success
  **closes** the breaker and normal traffic resumes automatically.
- Open and close are each reported ONCE per transition (log line +
  Sentry message), never per suppressed occurrence.

The single enforcement point is ``llm_client.acreate_with_failover`` /
the router's batch + stream paths — i.e. every routed runtime call.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

OPEN_KEY = "llm:credit_breaker:open"
PROBE_KEY = "llm:credit_breaker:probe_slot"

# str(exc) of the SDK's BadRequestError contains the API error message.
_CREDIT_MARKER = "credit balance is too low"


class LLMCallsSuspended(RuntimeError):
    """LLM work is paused: the credit-balance circuit breaker is open.

    Expected-state signal, not an error. Periodic jobs catch it (or let
    the Celery base task catch it) and defer their LLM work to a later
    tick without logging at ERROR level.
    """


def is_credit_error(exc: BaseException) -> bool:
    """True for the Anthropic 400 'credit balance is too low' error."""
    if getattr(exc, "status_code", None) != 400:
        # SDK BadRequestError always carries status_code=400; anything
        # without it (or with another status) is not a billing failure.
        return False
    return _CREDIT_MARKER in str(exc).lower()


# ── Redis plumbing (sync client — safe on any/no event loop) ─────────────

_redis_client: Optional[Any] = None
_redis_lock = threading.Lock()

# In-process fallback so the breaker still functions (per-worker) when
# Redis is unreachable.
_local_open = False
_local_probe_deadline = 0.0
# After a Redis failure, skip Redis for a short window so the breaker
# never adds connect-timeout latency to every LLM call while Redis is
# down — it degrades to per-process state instead.
_redis_down_until = 0.0
_REDIS_RETRY_SECONDS = 30.0


def _redis() -> Optional[Any]:
    if time.time() < _redis_down_until:
        return None
    return _get_redis()


def _mark_redis_down() -> None:
    global _redis_down_until
    _redis_down_until = time.time() + _REDIS_RETRY_SECONDS


def _probe_interval() -> int:
    from backend.app.config import get_settings

    return int(get_settings().LLM_BREAKER_PROBE_INTERVAL_SECONDS)


def _get_redis() -> Optional[Any]:
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            import redis as _redis

            from backend.app.config import get_settings

            url = get_settings().REDIS_URL
            kwargs = {
                "socket_timeout": 2,
                "socket_connect_timeout": 2,
            }
            if url.startswith("rediss://"):
                # Match the Celery broker's TLS posture (managed Redis
                # providers without a client CA bundle in the image).
                kwargs["ssl_cert_reqs"] = None
            _redis_client = _redis.Redis.from_url(url, **kwargs)
        except Exception:  # noqa: BLE001 — breaker degrades to local state
            logger.warning("llm breaker: redis client unavailable", exc_info=True)
            return None
    return _redis_client


def _report_transition(state: str, detail: str) -> None:
    """One log line + one Sentry message per state transition.

    WARNING level keeps the LoggingIntegration (event_level=ERROR) from
    double-reporting; the explicit capture_message is the single Sentry
    event.
    """
    logger.warning("LLM credit circuit breaker %s: %s", state.upper(), detail)
    try:
        import sentry_sdk

        sentry_sdk.capture_message(
            "LLM credit circuit breaker %s — %s" % (state, detail),
            level="warning" if state == "open" else "info",
        )
    except Exception:  # noqa: BLE001 — reporting must never break a call
        pass


# ── State transitions ─────────────────────────────────────────────────────


def is_open() -> bool:
    r = _redis()
    if r is not None:
        try:
            return bool(r.exists(OPEN_KEY))
        except Exception:  # noqa: BLE001
            logger.debug("llm breaker: redis read failed", exc_info=True)
            _mark_redis_down()
    return _local_open


def record_billing_failure(exc: BaseException) -> bool:
    """Open the breaker if ``exc`` is a credit-balance error.

    Returns True when the caller should raise :class:`LLMCallsSuspended`
    instead of the original exception. Only the transition that actually
    flips the shared state reports; concurrent workers stay silent.
    """
    global _local_open, _local_probe_deadline
    if not is_credit_error(exc):
        return False

    interval = _probe_interval()
    transitioned = False
    r = _redis()
    if r is not None:
        try:
            payload = json.dumps(
                {"opened_at": time.time(), "reason": str(exc)[:500]}
            )
            transitioned = bool(r.set(OPEN_KEY, payload, nx=True))
            if transitioned:
                # Fresh open: make probes wait a full interval.
                r.set(PROBE_KEY, "1", ex=interval)
        except Exception:  # noqa: BLE001
            logger.debug("llm breaker: redis open failed", exc_info=True)
            _mark_redis_down()
            transitioned = not _local_open
    else:
        transitioned = not _local_open
    _local_open = True
    _local_probe_deadline = time.time() + interval

    if transitioned:
        _report_transition(
            "open",
            "Anthropic credit balance exhausted; pausing all LLM calls "
            "(probe every %ss)" % interval,
        )
    return True


def close() -> None:
    """Close the breaker; reports once if this call made the transition."""
    global _local_open
    transitioned = False
    r = _redis()
    if r is not None:
        try:
            transitioned = bool(r.delete(OPEN_KEY))
            r.delete(PROBE_KEY)
        except Exception:  # noqa: BLE001
            logger.debug("llm breaker: redis close failed", exc_info=True)
            _mark_redis_down()
            transitioned = _local_open
    else:
        transitioned = _local_open
    _local_open = False
    if transitioned:
        _report_transition("close", "a probe call succeeded; resuming LLM calls")


def _claim_probe_slot() -> bool:
    """One probe per interval across the whole fleet (Redis NX lock)."""
    global _local_probe_deadline
    interval = _probe_interval()
    r = _redis()
    if r is not None:
        try:
            return bool(r.set(PROBE_KEY, "1", nx=True, ex=interval))
        except Exception:  # noqa: BLE001
            logger.debug("llm breaker: probe slot claim failed", exc_info=True)
            _mark_redis_down()
    now = time.time()
    if now >= _local_probe_deadline:
        _local_probe_deadline = now + interval
        return True
    return False


async def guard(client: Optional[Any] = None) -> None:
    """Gate one LLM call. No-op when the breaker is closed.

    While open: raises :class:`LLMCallsSuspended` — except for the one
    caller per interval that wins the probe slot, which issues a 1-token
    Haiku probe on ``client``. Probe success closes the breaker and lets
    the caller's real request proceed; probe failure keeps it open.
    """
    if not is_open():
        return
    if client is None or not _claim_probe_slot():
        raise LLMCallsSuspended(
            "LLM calls paused: Anthropic credit balance exhausted "
            "(circuit breaker open)"
        )
    from backend.app.services import model_catalog

    try:
        await client.messages.create(
            model=model_catalog.HAIKU,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:  # noqa: BLE001 — any probe failure keeps it open
        if is_credit_error(exc):
            raise LLMCallsSuspended(
                "LLM calls still paused: probe hit the credit-balance error"
            ) from None
        # Transient probe failure (network blip, 5xx): stay open quietly;
        # the next interval's probe retries.
        raise LLMCallsSuspended(
            "LLM calls paused: probe failed (%s)" % type(exc).__name__
        ) from exc
    close()


def _reset_for_tests() -> None:
    """Test hook: forget cached Redis client + local state."""
    global _redis_client, _local_open, _local_probe_deadline, _redis_down_until
    with _redis_lock:
        _redis_client = None
    _local_open = False
    _local_probe_deadline = 0.0
    _redis_down_until = 0.0


__all__ = [
    "LLMCallsSuspended",
    "is_credit_error",
    "is_open",
    "record_billing_failure",
    "close",
    "guard",
]
