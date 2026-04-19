"""Admin-only endpoints. Not exposed to end users.

Today this is limited to vector-health introspection. Auth gate reuses the
standard API key dependency — in production this route should be restricted to
admin tokens via an extra scope check, but for now any tenant with an API key
can inspect their own signals.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import KBChunk, Tenant
from backend.app.services.kb import ContextBuilderService, format_brief_for_prompt
from backend.app.services.kb.context_dispatch import schedule_context_rebuild
from backend.app.services.kb.vector_health import current_metrics, streak_days

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/admin/company-context")
async def get_company_context(
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """Return LINDA's current per-tenant company-context brief plus a
    rendered preview of how it lands in the system prompt."""
    brief = dict(tenant.company_context or {})
    return {
        "tenant_id": str(tenant.id),
        "brief": brief,
        "prompt_preview": format_brief_for_prompt(brief),
    }


@router.post("/admin/company-context/rebuild", status_code=202)
async def rebuild_company_context(
    mode: str = "full",
    sync: bool = False,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """Force a rebuild of the company-context brief.

    * ``mode=full`` (default) — stream every KB doc through the merger.
    * ``mode=debounce`` — just bump the debounce timer so an incremental
      merge runs shortly after the last KB write.
    * ``sync=true`` — run inline and return the new brief (blocks until done).
      Use for admin-driven rebuilds that want immediate feedback; leave false
      to offload to Celery.
    """
    if mode not in {"full", "debounce"}:
        mode = "full"

    if sync and mode == "full":
        builder = ContextBuilderService()
        brief = await builder.rebuild_all(db, tenant.id)
        return {"tenant_id": str(tenant.id), "mode": mode, "brief": brief}

    await schedule_context_rebuild(tenant.id, full=(mode == "full"))
    return {"tenant_id": str(tenant.id), "mode": mode, "scheduled": True}


@router.get("/admin/vector-health")
async def vector_health(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    """Vector store health snapshot for the developer.

    Returns the configured backend, chunk counts, query latency percentiles
    (rolling 24h), and the current alert streak so we can see at a glance
    whether pgvector is keeping up.
    """
    settings = get_settings()

    total_chunks = (await db.execute(select(func.count()).select_from(KBChunk))).scalar_one()
    tenant_chunks = (
        await db.execute(
            select(func.count()).select_from(KBChunk).where(KBChunk.tenant_id == tenant.id)
        )
    ).scalar_one()

    metrics = await current_metrics(total_chunks=int(total_chunks))
    streak = await streak_days()

    return {
        "backend": settings.VECTOR_BACKEND,
        "embed_model": settings.VOYAGE_EMBED_MODEL,
        "embed_dim": settings.VOYAGE_EMBED_DIM,
        "total_chunks": int(total_chunks),
        "tenant_chunks": int(tenant_chunks),
        "latency": {
            "p50_ms": metrics["p50_ms"],
            "p95_ms": metrics["p95_ms"],
            "p99_ms": metrics["p99_ms"],
            "samples_24h": int(metrics["samples_24h"]),
        },
        "thresholds": {
            "p95_ms": settings.VECTOR_HEALTH_P95_MS,
            "alert_days": settings.VECTOR_HEALTH_ALERT_DAYS,
            "size_milestones": settings.VECTOR_HEALTH_SIZE_MILESTONES,
        },
        "alert_streak_days": streak,
    }
