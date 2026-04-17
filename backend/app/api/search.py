"""Search API — full-text search over interactions via Elasticsearch."""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.app.auth import get_current_tenant
from backend.app.models import Tenant
from backend.app.services.search_service import SearchService

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class SearchHit(BaseModel):
    interaction_id: Optional[str] = None
    score: Optional[float] = None
    highlights: List[str] = []
    summary: str = ""
    channel: str = ""
    created_at: Optional[str] = None


class SearchResponse(BaseModel):
    results: List[SearchHit]
    count: int


# ── Singleton service instance ───────────────────────────

_search_service: Optional[SearchService] = None


def get_search_service() -> SearchService:
    global _search_service
    if _search_service is None:
        _search_service = SearchService()
    return _search_service


# ── Endpoints ────────────────────────────────────────────


@router.get("/search", response_model=SearchResponse)
async def search_interactions(
    q: str = Query(..., min_length=1, description="Search query"),
    channel: Optional[str] = Query(None, description="Filter by channel"),
    date_from: Optional[str] = Query(None, description="Start date (ISO format)"),
    date_to: Optional[str] = Query(None, description="End date (ISO format)"),
    agent_id: Optional[str] = Query(None, description="Filter by agent UUID"),
    limit: int = Query(20, ge=1, le=100, description="Max results to return"),
    tenant: Tenant = Depends(get_current_tenant),
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    """Full-text search across interactions for the current tenant.

    Returns matching interactions with highlighted transcript excerpts.
    """
    hits = await service.search(
        tenant_id=str(tenant.id),
        query=q,
        channel=channel,
        date_from=date_from,
        date_to=date_to,
        agent_id=agent_id,
        limit=limit,
    )
    return SearchResponse(
        results=[SearchHit(**h) for h in hits],
        count=len(hits),
    )
