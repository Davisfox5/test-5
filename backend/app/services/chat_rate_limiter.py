"""Redis-backed per-tenant rate limiter for Ask Linda.

Sliding-window counter — each tenant gets up to ``limit`` requests per
``window_seconds``. Cheap enough to run per chat turn; atomic via Redis
INCR + EXPIRE.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import redis.asyncio as aioredis

from backend.app.config import get_settings


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_s: int


class LindaRateLimiter:
    """Per-tenant sliding-window rate limiter backed by Redis."""

    def __init__(self, limit: int = 60, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._redis: aioredis.Redis | None = None

    def _client(self) -> aioredis.Redis:
        if self._redis is None:
            settings = get_settings()
            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis

    async def check(self, tenant_id: str) -> RateLimitResult:
        """Increment the counter for this tenant; return whether the caller is allowed."""
        now = int(time.time())
        bucket = now // self.window_seconds
        key = f"linda:chat:ratelimit:{tenant_id}:{bucket}"

        client = self._client()
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, self.window_seconds * 2)
        count, _ = await pipe.execute()

        remaining = max(0, self.limit - int(count))
        allowed = int(count) <= self.limit
        retry_after = 0 if allowed else ((bucket + 1) * self.window_seconds - now)
        return RateLimitResult(allowed=allowed, remaining=remaining, retry_after_s=retry_after)
