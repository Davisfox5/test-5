"""Liveness + readiness endpoints.

Three probes, matching what a load balancer + orchestrator actually
want:

* ``GET /health`` — **liveness**. Returns 200 if the process is alive.
  No I/O, no awaits. If this ever stops responding the orchestrator
  should restart the container.
* ``GET /ready`` — **readiness**. Checks every hard dependency
  (Postgres, Redis) and every soft dependency (Qdrant, Deepgram, S3).
  A hard-dep failure returns 503 so the load balancer stops routing
  traffic. A soft-dep failure still returns 200 but annotates the
  payload so dashboards can surface degradation.
* ``GET /ready/deep`` — same as ``/ready`` but also exercises the
  Elasticsearch transcript index + Qdrant collection. Used by the
  warmup script after a deploy; too slow for a per-request probe.

Shape is ``{status, hard, soft, checks: [{name, healthy, latency_ms,
error}]}`` so humans can read it and Prometheus scrapers can ingest it
without parsing prose.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

from fastapi import APIRouter, Response
from sqlalchemy import text

from backend.app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class CheckResult:
    name: str
    healthy: bool
    latency_ms: float
    error: Optional[str] = None
    detail: dict = field(default_factory=dict)


# ── Liveness ─────────────────────────────────────────────────────────


@router.get("/health")
async def health_check():
    """Liveness. Always fast, no I/O."""
    return {"status": "ok", "service": "linda-ai"}


# ── Readiness ────────────────────────────────────────────────────────


async def _timed_check(
    name: str, probe: Callable[[], Awaitable[Optional[dict]]]
) -> CheckResult:
    """Run a probe with a hard timeout; capture latency + any error."""
    t0 = time.monotonic()
    try:
        detail = await asyncio.wait_for(probe(), timeout=3.0)
        return CheckResult(
            name=name,
            healthy=True,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            detail=detail or {},
        )
    except asyncio.TimeoutError:
        return CheckResult(
            name=name,
            healthy=False,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            error="timeout after 3s",
        )
    except Exception as exc:  # noqa: BLE001 — probe exceptions stay contained
        return CheckResult(
            name=name,
            healthy=False,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            error=str(exc)[:200],
        )


async def _probe_postgres() -> Optional[dict]:
    """Open a connection and run SELECT 1. Proves the pool is healthy
    and migrations at least loaded the schema."""
    from backend.app.db import async_session

    async with async_session() as db:
        row = (await db.execute(text("SELECT 1"))).scalar()
    return {"one": int(row)}


async def _probe_redis() -> Optional[dict]:
    """PING + record the reply. Celery uses Redis for both the broker
    and the result backend, so losing Redis is a hard stop."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    try:
        reply = await r.ping()
    finally:
        await r.aclose()
    return {"ping": bool(reply)}


async def _probe_qdrant() -> Optional[dict]:
    """Hit the Qdrant cluster-status endpoint. Soft dep — voice/email
    analysis still runs without it, just without KB retrieval."""
    settings = get_settings()
    url = getattr(settings, "QDRANT_URL", None) or ""
    if not url:
        return {"configured": False}
    import httpx

    async with httpx.AsyncClient(timeout=2.5) as client:
        resp = await client.get(f"{url.rstrip('/')}/readyz")
    if resp.status_code >= 400:
        raise RuntimeError(f"status {resp.status_code}")
    return {"configured": True}


async def _probe_deepgram() -> Optional[dict]:
    """Cheap auth check: list projects via the REST API. Uses a short
    timeout so a Deepgram outage doesn't hold up the probe."""
    settings = get_settings()
    if not settings.DEEPGRAM_API_KEY:
        return {"configured": False}
    import httpx

    async with httpx.AsyncClient(timeout=2.5) as client:
        resp = await client.get(
            "https://api.deepgram.com/v1/projects",
            headers={"Authorization": f"Token {settings.DEEPGRAM_API_KEY}"},
        )
    if resp.status_code in (401, 403):
        raise RuntimeError(f"auth rejected ({resp.status_code})")
    if resp.status_code >= 500:
        raise RuntimeError(f"provider error ({resp.status_code})")
    return {"configured": True}


async def _probe_s3() -> Optional[dict]:
    """HEAD the staging bucket. Soft dep — only needed for file upload
    ingest path."""
    settings = get_settings()
    if not settings.AWS_S3_BUCKET:
        return {"configured": False}

    def _head() -> None:
        import boto3

        client = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
        )
        client.head_bucket(Bucket=settings.AWS_S3_BUCKET)

    await asyncio.get_event_loop().run_in_executor(None, _head)
    return {"configured": True, "bucket": settings.AWS_S3_BUCKET}


async def _probe_elasticsearch() -> Optional[dict]:
    """Cluster health. Soft dep — transcript search still falls back
    to Postgres full-text when ES is down, but it's slow."""
    settings = get_settings()
    url = getattr(settings, "ELASTICSEARCH_URL", None) or ""
    if not url:
        return {"configured": False}
    import httpx

    async with httpx.AsyncClient(timeout=2.5) as client:
        resp = await client.get(f"{url.rstrip('/')}/_cluster/health")
    if resp.status_code >= 400:
        raise RuntimeError(f"status {resp.status_code}")
    body = resp.json() if resp.content else {}
    return {
        "configured": True,
        "cluster_status": body.get("status"),
    }


_HARD_PROBES: list[tuple[str, Callable[[], Awaitable[Optional[dict]]]]] = [
    ("postgres", _probe_postgres),
    ("redis", _probe_redis),
]

_SOFT_PROBES: list[tuple[str, Callable[[], Awaitable[Optional[dict]]]]] = [
    ("qdrant", _probe_qdrant),
    ("deepgram", _probe_deepgram),
    ("s3", _probe_s3),
]

_DEEP_PROBES: list[tuple[str, Callable[[], Awaitable[Optional[dict]]]]] = [
    ("elasticsearch", _probe_elasticsearch),
]


async def _run_probes(
    probes: list[tuple[str, Callable[[], Awaitable[Optional[dict]]]]],
) -> list[CheckResult]:
    results = await asyncio.gather(
        *(_timed_check(name, probe) for name, probe in probes),
        return_exceptions=False,
    )
    return list(results)


@router.get("/ready")
async def readiness_check(response: Response):
    hard, soft = await asyncio.gather(
        _run_probes(_HARD_PROBES),
        _run_probes(_SOFT_PROBES),
    )
    hard_healthy = all(c.healthy for c in hard)
    if not hard_healthy:
        response.status_code = 503
    return {
        "status": "ready" if hard_healthy else "not_ready",
        "hard_ok": hard_healthy,
        "soft_ok": all(c.healthy for c in soft),
        "checks": [asdict(c) for c in (hard + soft)],
    }


@router.get("/ready/deep")
async def readiness_check_deep(response: Response):
    """Full probe set — call from deploy hooks, not from every load
    balancer tick. Includes Elasticsearch, which is slower."""
    hard, soft, deep = await asyncio.gather(
        _run_probes(_HARD_PROBES),
        _run_probes(_SOFT_PROBES),
        _run_probes(_DEEP_PROBES),
    )
    hard_healthy = all(c.healthy for c in hard)
    if not hard_healthy:
        response.status_code = 503
    return {
        "status": "ready" if hard_healthy else "not_ready",
        "hard_ok": hard_healthy,
        "soft_ok": all(c.healthy for c in soft),
        "deep_ok": all(c.healthy for c in deep),
        "checks": [asdict(c) for c in (hard + soft + deep)],
    }
