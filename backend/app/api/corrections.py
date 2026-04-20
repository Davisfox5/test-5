"""Active-learning corrections — surface uncertain scorer outputs and
accept user corrections that grow the golden evaluation set.

Two endpoints:

- ``GET /corrections/queue`` — returns the most uncertain recent items
  for the requested scorer.  Uncertainty ranked by ensemble disagreement
  (when both categorical and numeric signals exist and disagree) and by
  how close the calibrated probability is to 0.5.
- ``POST /corrections`` — accepts one correction and writes a
  :class:`CorrectionEvent` row.  The weekly calibration job reads these
  corrections as soft-labels and gives them extra weight.

The behavior is deliberately non-blocking: users are never forced to
correct anything.  The queue grows naturally as scoring happens and
shrinks as corrections come in.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import (
    CorrectionEvent,
    InteractionFeatures,
    Interaction,
    Tenant,
)
from backend.app.services import score_engine

router = APIRouter()


# ── Pydantic ─────────────────────────────────────────────────────────────


class QueueItem(BaseModel):
    interaction_id: uuid.UUID
    title: Optional[str] = None
    target_type: str  # 'sentiment' | 'churn_risk'
    original: Dict[str, Any]
    uncertainty: float  # 0–1, higher = more uncertain


class CorrectionIn(BaseModel):
    interaction_id: Optional[uuid.UUID] = None
    target_type: str
    target_id: Optional[str] = None
    original: Dict[str, Any]
    correction: Dict[str, Any]
    note: Optional[str] = None


class CorrectionOut(BaseModel):
    id: uuid.UUID
    created_at: datetime


# ── Helpers ──────────────────────────────────────────────────────────────


def _uncertainty_for_sentiment(features_row: Any) -> float:
    """Large when score is near mid-range and LLM/trajectory disagree."""
    llm = features_row.llm_structured or {}
    raw = llm.get("sentiment_score")
    if raw is None:
        return 0.0
    # Normalize 0–10 → distance from the poles; peak uncertainty at 5.
    mid_dist = 1.0 - abs(float(raw) - 5.0) / 5.0
    # Agreement between categorical and numeric signals.
    cat = (llm.get("sentiment_overall") or "").lower()
    expected_cat = "positive" if float(raw) >= 6.5 else "negative" if float(raw) <= 3.5 else "neutral"
    disagree = 0.0 if cat == expected_cat or not cat else 0.5
    return min(1.0, 0.6 * mid_dist + disagree)


def _uncertainty_for_churn(features_row: Any) -> float:
    """High when the numeric churn_risk sits near 0.5 or lacks context."""
    llm = features_row.llm_structured or {}
    raw = llm.get("churn_risk")
    if raw is None:
        return 0.0
    d = 1.0 - abs(float(raw) - 0.5) * 2
    signal = (llm.get("churn_risk_signal") or "").lower()
    bucket_expected = {
        "high": raw >= 0.7,
        "medium": 0.4 <= raw < 0.7,
        "low": 0.1 <= raw < 0.4,
        "none": raw < 0.1,
    }.get(signal, True)
    return min(1.0, 0.7 * d + (0.0 if bucket_expected else 0.4))


# ── Queue endpoint ───────────────────────────────────────────────────────


@router.get("/corrections/queue", response_model=List[QueueItem])
async def correction_queue(
    target_type: str = Query("sentiment", pattern="^(sentiment|churn_risk)$"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Most-uncertain recent items for the requested scorer."""
    stmt = (
        select(InteractionFeatures, Interaction)
        .join(Interaction, Interaction.id == InteractionFeatures.interaction_id)
        .where(InteractionFeatures.tenant_id == tenant.id)
        .order_by(desc(Interaction.created_at))
        .limit(200)
    )
    candidates = (await db.execute(stmt)).all()

    scored: List[QueueItem] = []
    for features_row, interaction in candidates:
        if target_type == "sentiment":
            uncertainty = _uncertainty_for_sentiment(features_row)
            original = {
                "sentiment_score": (features_row.llm_structured or {}).get("sentiment_score"),
                "sentiment_overall": (features_row.llm_structured or {}).get("sentiment_overall"),
            }
        else:
            uncertainty = _uncertainty_for_churn(features_row)
            original = {
                "churn_risk": (features_row.llm_structured or {}).get("churn_risk"),
                "churn_risk_signal": (features_row.llm_structured or {}).get("churn_risk_signal"),
            }
        if uncertainty <= 0.1:
            continue
        scored.append(QueueItem(
            interaction_id=features_row.interaction_id,
            title=getattr(interaction, "title", None),
            target_type=target_type,
            original=original,
            uncertainty=round(uncertainty, 3),
        ))

    scored.sort(key=lambda q: q.uncertainty, reverse=True)
    return scored[:limit]


# ── Submit correction ────────────────────────────────────────────────────


@router.post("/corrections", response_model=CorrectionOut, status_code=201)
async def submit_correction(
    body: CorrectionIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    row = CorrectionEvent(
        tenant_id=tenant.id,
        interaction_id=body.interaction_id,
        target_type=body.target_type,
        target_id=body.target_id,
        original=body.original,
        correction=body.correction,
        note=body.note,
    )
    db.add(row)
    await db.flush()
    await db.commit()
    return CorrectionOut(id=row.id, created_at=row.created_at)


@router.get("/corrections", response_model=List[Dict[str, Any]])
async def list_corrections(
    target_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = (
        select(CorrectionEvent)
        .where(CorrectionEvent.tenant_id == tenant.id)
        .order_by(desc(CorrectionEvent.created_at))
        .limit(limit)
    )
    if target_type:
        stmt = stmt.where(CorrectionEvent.target_type == target_type)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "interaction_id": str(r.interaction_id) if r.interaction_id else None,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "original": r.original or {},
            "correction": r.correction or {},
            "note": r.note,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
