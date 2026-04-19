"""Daily vector-health checker.

Runs once a day (via Celery beat or an equivalent scheduler) to:

* Compute the rolling 24h p95 latency.
* Update the sustained-breach streak counter.
* Emit a WARN log tagged ``[VECTOR_HEALTH_ALERT]`` when either the streak
  threshold is crossed or a new size milestone is hit.
* Optionally file a GitHub issue on the configured repo when an alert fires,
  so the developer gets a passive notification at zero infra cost.

The actual scheduler wiring lives in ``backend.app.tasks`` — this module just
exposes the ``run_vector_health_check`` coroutine.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import KBChunk
from backend.app.services.kb.vector_health import (
    alert_log,
    current_metrics,
    mark_milestone_alerted,
    milestone_already_alerted,
    update_streak,
)

logger = logging.getLogger(__name__)


async def run_vector_health_check(db: AsyncSession) -> Dict[str, Any]:
    """Run one cycle. Returns a summary the scheduler can log or publish."""
    settings = get_settings()

    total_chunks = int(
        (await db.execute(select(func.count()).select_from(KBChunk))).scalar_one()
    )
    metrics = await current_metrics(total_chunks=total_chunks)
    p95 = metrics["p95_ms"]

    streak = await update_streak(p95, settings.VECTOR_HEALTH_P95_MS)

    alerts: list[dict] = []

    # Latency streak alert.
    if streak >= settings.VECTOR_HEALTH_ALERT_DAYS:
        msg = (
            f"pgvector p95 latency {p95:.1f}ms has exceeded "
            f"{settings.VECTOR_HEALTH_P95_MS}ms for {streak} consecutive days "
            f"(backend={settings.VECTOR_BACKEND}, total_chunks={total_chunks})."
        )
        alert_log(msg)
        alerts.append({"kind": "latency_streak", "message": msg})

    # Size milestone alert — only fire each milestone once.
    for milestone in settings.VECTOR_HEALTH_SIZE_MILESTONES:
        if total_chunks >= milestone and not await milestone_already_alerted(milestone):
            msg = (
                f"KB chunk count hit {total_chunks} (milestone {milestone}). "
                f"Consider migrating VECTOR_BACKEND to qdrant."
            )
            alert_log(msg)
            await mark_milestone_alerted(milestone)
            alerts.append({"kind": "size_milestone", "milestone": milestone, "message": msg})

    if alerts and settings.GITHUB_ALERT_REPO and settings.GITHUB_ALERT_TOKEN:
        await _file_github_issue(settings, alerts, metrics, total_chunks)

    return {
        "total_chunks": total_chunks,
        "p95_ms": p95,
        "streak_days": streak,
        "alerts": alerts,
    }


async def _file_github_issue(
    settings,
    alerts: list[dict],
    metrics: Dict[str, Any],
    total_chunks: int,
) -> None:
    """POST a GitHub issue on sustained breach. Zero-cost notification path."""
    title = "[vector-health] pgvector thresholds crossed"
    body_lines = [
        "Vector-health check fired the following alert(s):",
        "",
    ]
    for a in alerts:
        body_lines.append(f"- **{a['kind']}** — {a['message']}")
    body_lines += [
        "",
        "## Current metrics",
        "```json",
        json.dumps(
            {"total_chunks": total_chunks, **metrics, "backend": settings.VECTOR_BACKEND},
            indent=2,
        ),
        "```",
        "",
        "## Suggested next steps",
        "1. Flip `VECTOR_BACKEND=qdrant` once a Qdrant instance is provisioned.",
        "2. Run `POST /api/v1/kb/reindex` per tenant to populate the new backend.",
        "3. Close this issue after the p95 drops back under threshold.",
    ]
    body = "\n".join(body_lines)

    url = f"https://api.github.com/repos/{settings.GITHUB_ALERT_REPO}/issues"
    headers = {
        "Authorization": f"Bearer {settings.GITHUB_ALERT_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "title": title,
        "body": body,
        "labels": ["infra:scaling", "auto-generated"],
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 300:
                logger.warning(
                    "GitHub alert issue POST failed: %s %s", resp.status_code, resp.text
                )
    except httpx.HTTPError:
        logger.exception("GitHub alert issue POST errored")
