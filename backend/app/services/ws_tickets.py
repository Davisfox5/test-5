"""WebSocket ticket service — auth handshake + rate limiting.

Browsers can't attach custom headers to a ``WebSocket`` constructor, so
a Bearer token can't be sent on the actual WebSocket open.  The standard
workaround is a **ticket handshake**:

1. Client calls ``POST /ws/tickets`` over HTTPS with its normal Bearer
   token and a desired ``session_id`` + ``role`` (``agent`` or
   ``monitor``).
2. Server validates the bearer, generates a short-lived single-use
   ticket bound to ``(tenant_id, session_id, role)``, and stores it in
   Redis with a 120-second TTL.
3. Client opens ``wss://…/ws/live/{session_id}?ticket=<ticket>``.
4. The WebSocket handler consumes the ticket (delete-on-read) before
   calling ``accept()``; any mismatch (expired, already used, wrong
   tenant, wrong session, wrong role) closes the connection with code
   4401.

Tickets are pure Redis — no DB table — so there's no schema change and
expiry is free via Redis TTL.  Single-use is enforced by ``DELETE`` on
consume; a valid ticket cannot be replayed even inside its TTL window.

Rate limiting is a separate Redis sliding-window counter keyed on the
caller's API-key hash, with a 60 s fixed window.  Reset is automatic
via TTL; no cleanup task needed.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────────


DEFAULT_TICKET_TTL_SEC = 120
MAX_CONNECTIONS_PER_MINUTE = 30


# ── Errors ───────────────────────────────────────────────────────────────


class WebSocketAuthError(Exception):
    """Raised when a ticket is missing, expired, already consumed, or
    refers to a different (tenant, session, role) than the handler
    expected.  Handlers translate this into close code 4401.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RateLimitedError(WebSocketAuthError):
    """Raised when a caller exceeds the per-key new-connection quota.
    Handlers close with 4429.
    """

    def __init__(self, reason: str = "rate_limited") -> None:
        super().__init__(reason)


# ── Keys ─────────────────────────────────────────────────────────────────


def _ticket_key(ticket: str) -> str:
    return f"wsticket:{ticket}"


def _rate_key(api_key_hash: str, minute_bucket: int) -> str:
    return f"wsrate:{api_key_hash}:{minute_bucket}"


# ── Ticket shape ─────────────────────────────────────────────────────────


@dataclass
class Ticket:
    tenant_id: str
    session_id: str
    role: str          # "agent" | "monitor"
    user_id: Optional[str] = None
    issued_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps({
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "role": self.role,
            "user_id": self.user_id,
            "issued_at": self.issued_at,
        })

    @classmethod
    def from_json(cls, raw: str) -> "Ticket":
        data = json.loads(raw)
        return cls(
            tenant_id=data["tenant_id"],
            session_id=data["session_id"],
            role=data["role"],
            user_id=data.get("user_id"),
            issued_at=float(data.get("issued_at", 0.0)),
        )


# ── Issue / consume ──────────────────────────────────────────────────────


async def issue_ticket(
    redis: Any,
    *,
    tenant_id: str,
    session_id: str,
    role: str,
    user_id: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TICKET_TTL_SEC,
) -> Dict[str, Any]:
    """Mint a single-use ticket bound to one connection.

    Returns ``{"ticket": str, "expires_at": float, "session_id": str}``.
    ``redis`` is any object exposing async ``setex``; the FastAPI path
    passes a real ``redis.asyncio.Redis``, tests pass an in-memory stub.
    """
    if role not in {"agent", "monitor"}:
        raise WebSocketAuthError("invalid_role")
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    payload = Ticket(
        tenant_id=tenant_id,
        session_id=session_id,
        role=role,
        user_id=user_id,
        issued_at=now,
    )
    await redis.setex(_ticket_key(ticket), ttl_seconds, payload.to_json())
    return {
        "ticket": ticket,
        "expires_at": now + ttl_seconds,
        "session_id": session_id,
        "role": role,
    }


async def consume_ticket(
    redis: Any,
    ticket: Optional[str],
    *,
    expected_tenant_id: Optional[str] = None,
    expected_session_id: Optional[str] = None,
    expected_role: Optional[str] = None,
) -> Ticket:
    """Validate + delete the ticket atomically.

    Any mismatch or absence raises :class:`WebSocketAuthError` with a
    specific reason (``missing``, ``invalid``, ``expired``,
    ``wrong_tenant``, ``wrong_session``, ``wrong_role``) suitable for
    logging but **not** exposing to the client.
    """
    if not ticket:
        raise WebSocketAuthError("missing")

    # Atomic read + delete via pipelined GETDEL, with a best-effort
    # fallback for redis clients without GETDEL (older versions / our
    # in-memory test stub).
    raw: Optional[Any] = None
    try:
        raw = await redis.getdel(_ticket_key(ticket))
    except AttributeError:
        raw = await redis.get(_ticket_key(ticket))
        if raw is not None:
            try:
                await redis.delete(_ticket_key(ticket))
            except Exception:  # noqa: BLE001
                pass

    if raw is None:
        raise WebSocketAuthError("expired_or_consumed")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload = Ticket.from_json(raw)
    except (ValueError, KeyError):
        raise WebSocketAuthError("invalid")

    if expected_tenant_id is not None and payload.tenant_id != expected_tenant_id:
        raise WebSocketAuthError("wrong_tenant")
    if expected_session_id is not None and payload.session_id != expected_session_id:
        raise WebSocketAuthError("wrong_session")
    if expected_role is not None and payload.role != expected_role:
        raise WebSocketAuthError("wrong_role")
    return payload


# ── Rate limiting ────────────────────────────────────────────────────────


async def enforce_new_connection_quota(
    redis: Any,
    api_key_hash: str,
    *,
    limit: int = MAX_CONNECTIONS_PER_MINUTE,
) -> int:
    """Increment the per-minute counter for ``api_key_hash``; raise
    :class:`RateLimitedError` when over ``limit``.

    Returns the post-increment count (useful for logging).  Uses a fixed
    1-minute window; bucketing happens automatically through the key.
    """
    minute_bucket = int(time.time() // 60)
    key = _rate_key(api_key_hash, minute_bucket)
    try:
        count = await redis.incr(key)
        if count == 1:
            # First bump in this bucket — set expiry so the key goes away.
            try:
                await redis.expire(key, 60)
            except Exception:  # noqa: BLE001
                pass
    except AttributeError:
        # Test stub without INCR/EXPIRE.  Simulate via get/set.
        raw = await redis.get(key)
        count = (int(raw) if raw else 0) + 1
        await redis.setex(key, 60, str(count))
    if count > limit:
        raise RateLimitedError()
    return int(count)


# ── Lightweight in-memory Redis for tests ────────────────────────────────


class InMemoryRedisStub:
    """Minimal async Redis substitute for unit tests.

    Implements just the surface the ticket service uses: ``setex``,
    ``get``, ``delete``, ``getdel``, ``incr``, ``expire``.  Not
    thread-safe; one instance per test.
    """

    def __init__(self) -> None:
        self._store: Dict[str, Any] = {}
        self._expiries: Dict[str, float] = {}

    async def setex(self, key: str, ttl: int, value: Any) -> None:
        self._store[key] = value
        self._expiries[key] = time.time() + ttl

    async def get(self, key: str) -> Any:
        if key in self._expiries and self._expiries[key] < time.time():
            self._store.pop(key, None)
            self._expiries.pop(key, None)
        return self._store.get(key)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self._expiries.pop(key, None)

    async def getdel(self, key: str) -> Any:
        val = await self.get(key)
        if val is not None:
            await self.delete(key)
        return val

    async def incr(self, key: str) -> int:
        cur = int(self._store.get(key, 0)) + 1
        self._store[key] = cur
        return cur

    async def expire(self, key: str, ttl: int) -> None:
        if key in self._store:
            self._expiries[key] = time.time() + ttl
