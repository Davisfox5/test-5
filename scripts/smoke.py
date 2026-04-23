"""End-to-end smoke test harness.

Walks through the critical paths a real tenant uses, against a
staging deploy with real dependencies (Deepgram / HubSpot / Pipedrive
sandbox accounts, etc.). Designed to run post-deploy so production
bugs surface before real traffic hits them.

Run against a staging URL::

    python scripts/smoke.py \\
        --base-url https://staging.linda.example.com \\
        --api-key $LINDA_STAGING_API_KEY \\
        --audio-url https://example.com/sample-call.wav

Exit code 0 when every selected check passes, 1 otherwise. Use
``--skip`` to disable specific checks when a sandbox account isn't
available::

    python scripts/smoke.py --base-url … --api-key … --skip pipedrive,salesforce

Checks ship individually so new ones can slot in without bloating
the existing ones; each returns a :class:`SmokeResult` with a
healthy flag + latency + any error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    print("httpx required — pip install httpx", file=sys.stderr)
    raise


@dataclass
class SmokeResult:
    name: str
    healthy: bool
    latency_ms: float
    detail: Dict[str, object] = field(default_factory=dict)
    error: Optional[str] = None


def _timed(
    name: str, fn: Callable[[], SmokeResult]
) -> SmokeResult:
    t0 = time.monotonic()
    try:
        result = fn()
        result.latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return result
    except Exception as exc:
        return SmokeResult(
            name=name,
            healthy=False,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )


# ── Individual checks ───────────────────────────────────────────────


def check_health(client: httpx.Client, base_url: str) -> SmokeResult:
    resp = client.get(f"{base_url}/api/v1/health")
    resp.raise_for_status()
    body = resp.json()
    return SmokeResult(
        name="health",
        healthy=(body.get("status") == "ok"),
        latency_ms=0.0,
        detail=body,
    )


def check_ready(client: httpx.Client, base_url: str) -> SmokeResult:
    resp = client.get(f"{base_url}/api/v1/ready")
    body = resp.json()
    return SmokeResult(
        name="ready",
        healthy=(resp.status_code == 200 and body.get("hard_ok")),
        latency_ms=0.0,
        detail={
            "soft_ok": body.get("soft_ok"),
            "failed_checks": [
                c["name"] for c in body.get("checks", []) if not c["healthy"]
            ],
        },
    )


def check_auth(client: httpx.Client, base_url: str) -> SmokeResult:
    """Hit a bearer-auth-gated endpoint to prove the API key works."""
    resp = client.get(f"{base_url}/api/v1/me")
    resp.raise_for_status()
    body = resp.json()
    return SmokeResult(
        name="auth",
        healthy=bool(body.get("tenant") and body.get("user")),
        latency_ms=0.0,
        detail={"tenant": body.get("tenant", {}).get("name"),
                "role": body.get("user", {}).get("role")},
    )


def check_ingest_recording(
    client: httpx.Client, base_url: str, audio_url: str
) -> SmokeResult:
    """Post a URL-mode recording ingest and verify the interaction
    record lands + reaches a terminal state."""
    resp = client.post(
        f"{base_url}/api/v1/interactions/ingest-recording",
        json={
            "audio_url": audio_url,
            "title": "smoke-test ingest",
            "source": "smoke-harness",
            "external_call_id": f"smoke-{int(time.time())}",
            "engine": "deepgram",
        },
    )
    if resp.status_code >= 400:
        return SmokeResult(
            name="ingest_recording",
            healthy=False,
            latency_ms=0.0,
            error=f"status {resp.status_code}: {resp.text[:200]}",
        )
    interaction = resp.json()
    interaction_id = interaction["id"]

    # Poll up to 2 minutes for the async pipeline to land insights.
    # Staging runs on CPU-only workers so be generous.
    deadline = time.monotonic() + 120
    terminal_status = None
    while time.monotonic() < deadline:
        g = client.get(f"{base_url}/api/v1/interactions/{interaction_id}")
        g.raise_for_status()
        got = g.json()
        status = got.get("status")
        if status in ("analyzed", "failed", "transcription_failed"):
            terminal_status = status
            break
        time.sleep(5)

    return SmokeResult(
        name="ingest_recording",
        healthy=(terminal_status == "analyzed"),
        latency_ms=0.0,
        detail={
            "interaction_id": interaction_id,
            "final_status": terminal_status,
        },
    )


def check_kb_search(client: httpx.Client, base_url: str) -> SmokeResult:
    resp = client.get(
        f"{base_url}/api/v1/kb/search",
        params={"q": "refund policy", "limit": 3},
    )
    if resp.status_code >= 400:
        return SmokeResult(
            name="kb_search",
            healthy=False,
            latency_ms=0.0,
            error=f"status {resp.status_code}",
        )
    return SmokeResult(
        name="kb_search",
        healthy=True,
        latency_ms=0.0,
        detail={"results": len(resp.json() or [])},
    )


def check_crm(
    client: httpx.Client, base_url: str, provider: str
) -> SmokeResult:
    """Trigger a CRM sync and inspect the latest log row."""
    resp = client.post(f"{base_url}/api/v1/crm/sync/{provider}")
    if resp.status_code >= 400:
        return SmokeResult(
            name=f"crm_{provider}",
            healthy=False,
            latency_ms=0.0,
            error=f"dispatch status {resp.status_code}",
        )
    # Poll the log endpoint until we see a terminal status for this
    # provider.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        g = client.get(
            f"{base_url}/api/v1/crm/sync/logs",
            params={"provider": provider, "limit": 1},
        )
        if g.status_code == 200 and g.json():
            latest = g.json()[0]
            status = latest.get("status")
            if status in ("success", "partial", "failed"):
                return SmokeResult(
                    name=f"crm_{provider}",
                    healthy=(status in ("success", "partial")),
                    latency_ms=0.0,
                    detail={
                        "status": status,
                        "customers": latest.get("customers_upserted"),
                        "contacts": latest.get("contacts_upserted"),
                        "deals": latest.get("deals_upserted", 0),
                    },
                )
        time.sleep(5)
    return SmokeResult(
        name=f"crm_{provider}",
        healthy=False,
        latency_ms=0.0,
        error="sync did not finish within 3 min",
    )


# ── Orchestration ───────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="Staging base URL")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("LINDA_API_KEY"),
        help="API key with admin role (env: LINDA_API_KEY).",
    )
    parser.add_argument(
        "--audio-url",
        default=None,
        help="Public URL to a WAV/MP3 for the ingest-recording smoke.",
    )
    parser.add_argument(
        "--skip",
        default="",
        help="Comma-separated check names to skip.",
    )
    parser.add_argument(
        "--crm",
        default="",
        help="Comma-separated CRM providers to sync (pipedrive,hubspot,salesforce).",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.api_key:
        print("--api-key or $LINDA_API_KEY required", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    crms = [s.strip() for s in args.crm.split(",") if s.strip()]

    results: List[SmokeResult] = []
    with httpx.Client(
        timeout=30.0,
        headers={"Authorization": f"Bearer {args.api_key}"},
    ) as client:
        if "health" not in skip:
            results.append(_timed("health", lambda: check_health(client, base_url)))
        if "ready" not in skip:
            results.append(_timed("ready", lambda: check_ready(client, base_url)))
        if "auth" not in skip:
            results.append(_timed("auth", lambda: check_auth(client, base_url)))
        if "ingest_recording" not in skip and args.audio_url:
            results.append(
                _timed(
                    "ingest_recording",
                    lambda: check_ingest_recording(client, base_url, args.audio_url),
                )
            )
        if "kb_search" not in skip:
            results.append(
                _timed("kb_search", lambda: check_kb_search(client, base_url))
            )
        for provider in crms:
            if f"crm_{provider}" in skip:
                continue
            results.append(
                _timed(
                    f"crm_{provider}",
                    lambda p=provider: check_crm(client, base_url, p),
                )
            )

    payload = {
        "base_url": base_url,
        "results": [asdict(r) for r in results],
        "all_healthy": all(r.healthy for r in results),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for r in results:
            icon = "✓" if r.healthy else "✗"
            extras = ""
            if r.detail:
                extras = f"  {r.detail}"
            if r.error:
                extras = f"  {r.error}"
            print(f"{icon}  {r.name:22s}  {r.latency_ms:>7.1f} ms{extras}")
        print()
        print("OK" if payload["all_healthy"] else "FAIL")
    return 0 if payload["all_healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
