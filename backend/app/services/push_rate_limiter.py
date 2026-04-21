"""Tiny Redis-backed fixed-window rate limiter.

Used to shield the push-notification endpoints (Gmail Pub/Sub, Graph
webhooks) from abuse while staying out of the request hot path under
normal load.  Redis is the truth source when configured; if unreachable,
an in-process fallback keeps things moving — with the known caveat that
multi-process deployments will limit per-process rather than globally.
That's good enough for DoS mitigation, bad enough that we log when it
kicks in.

Usage::

    limiter = get_limiter()
    allowed, remaining, reset_seconds = limiter.check(
        key=f"gmail-push:{client_ip}", limit=60, window_seconds=60
    )
    if not allowed:
        raise HTTPException(429, ...)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class _LocalBucket:
    count: int = 0
    reset_at: float = 0.0


@dataclass
class _LocalState:
    buckets: Dict[str, _LocalBucket] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class RateLimiter:
    def __init__(self) -> None:
        self._local = _LocalState()
        self._redis = None
        self._redis_checked = False

    def _get_redis(self):
        if self._redis_checked:
            return self._redis
        self._redis_checked = True
        try:
            import redis

            self._redis = redis.from_url(
                get_settings().REDIS_URL,
                socket_connect_timeout=0.25,
                socket_timeout=0.25,
                decode_responses=True,
            )
            # Ping to confirm — failure drops us to the local fallback.
            self._redis.ping()
        except Exception:
            logger.info("Rate limiter falling back to in-process store (no Redis)")
            self._redis = None
        return self._redis

    def check(
        self, key: str, limit: int, window_seconds: int
    ) -> Tuple[bool, int, int]:
        """Return ``(allowed, remaining, reset_in_seconds)``."""
        now = int(time.time())
        window_start = now - (now % window_seconds)
        reset_at = window_start + window_seconds
        bucket_key = f"rl:{key}:{window_start}"

        client = self._get_redis()
        if client is not None:
            try:
                pipe = client.pipeline()
                pipe.incr(bucket_key)
                pipe.expire(bucket_key, window_seconds + 5)
                count, _ = pipe.execute()
                count = int(count)
                allowed = count <= limit
                remaining = max(0, limit - count)
                return allowed, remaining, max(0, reset_at - now)
            except Exception:
                logger.exception(
                    "Redis rate-limit check failed; using in-process bucket"
                )

        # Local fallback.
        with self._local.lock:
            bucket = self._local.buckets.get(key)
            if bucket is None or bucket.reset_at <= now:
                bucket = _LocalBucket(count=0, reset_at=reset_at)
                self._local.buckets[key] = bucket
            bucket.count += 1
            allowed = bucket.count <= limit
            remaining = max(0, limit - bucket.count)
            return allowed, remaining, max(0, int(bucket.reset_at) - now)


_limiter: Optional[RateLimiter] = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
