"""Evaluation + reference-set + vocabulary-candidate management API.

- POST /evaluation/trigger/{interaction_id}     — manually trigger judges
- GET  /evaluation/scores/{interaction_id}      — read all dimension scores
- POST /evaluation/reference-sets               — curate / freeze a reference set
- GET  /evaluation/reference-sets               — list
- POST /evaluation/vocabulary/{cand_id}/approve — Gate 1
- POST /evaluation/vocabulary/{cand_id}/reject  — Gate 1
- GET  /evaluation/vocabulary                   — list pending candidates
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import (
    EvaluationReferenceSet,
    InsightQualityScore,
    Interaction,
    Tenant,
    VocabularyCandidate,
)

router = APIRouter()


# ── Trigger judges manually (admin only — no auth ranking yet) ───────────


@router.post("/evaluation/trigger/{interaction_id}", status_code=202)
async def trigger_evaluation(
    interaction_id: uuid.UUID,
    surface: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Enqueue a judge run for a single interaction.

    ``surface`` is one of 'analysis' | 'email_classifier' | 'email_reply'.
    """
    if surface not in ("analysis", "email_classifier", "email_reply"):
        raise HTTPException(status_code=400, detail="Unknown surface")
    interaction = (
        await db.execute(
            select(Interaction).where(
                Interaction.id == interaction_id,
                Interaction.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if interaction is None:
        raise HTTPException(status_code=404, detail="Interaction not found")

    from backend.app.tasks import (
        evaluate_analysis,
        evaluate_classification,
        evaluate_reply,
    )

    if surface == "analysis":
        evaluate_analysis.delay(str(interaction_id))
    elif surface == "email_classifier":
        evaluate_classification.delay(str(interaction_id))
    else:
        evaluate_reply.delay(str(interaction_id))
    return {"status": "queued", "surface": surface}


# ── Read scores ──────────────────────────────────────────────────────────


class ScoreOut(BaseModel):
    dimension: str
    score: float
    reasoning: Optional[str]
    evaluator_type: str
    evaluator_id: str
    surface: str
    created_at: datetime
    prompt_variant_id: Optional[uuid.UUID]


@router.get("/evaluation/scores/{interaction_id}", response_model=List[ScoreOut])
async def get_scores(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    rows = (
        await db.execute(
            select(InsightQualityScore)
            .where(
                InsightQualityScore.interaction_id == interaction_id,
                InsightQualityScore.tenant_id == tenant.id,
            )
            .order_by(InsightQualityScore.created_at.desc())
        )
    ).scalars().all()
    return [
        ScoreOut(
            dimension=r.dimension,
            score=r.score,
            reasoning=r.reasoning,
            evaluator_type=r.evaluator_type,
            evaluator_id=r.evaluator_id,
            surface=r.surface,
            created_at=r.created_at,
            prompt_variant_id=r.prompt_variant_id,
        )
        for r in rows
    ]


# ── Reference sets ───────────────────────────────────────────────────────


class ReferenceSetCreate(BaseModel):
    surface: str
    name: str
    interaction_ids: List[uuid.UUID] = Field(default_factory=list)
    reference_outputs: dict = Field(default_factory=dict)


class ReferenceSetOut(BaseModel):
    id: uuid.UUID
    tenant_id: Optional[uuid.UUID]
    surface: str
    name: str
    version: int
    interaction_ids: list
    created_at: datetime
    frozen_at: Optional[datetime]


@router.post(
    "/evaluation/reference-sets", response_model=ReferenceSetOut, status_code=201
)
async def create_reference_set(
    body: ReferenceSetCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    rs = EvaluationReferenceSet(
        tenant_id=tenant.id,
        surface=body.surface,
        name=body.name,
        interaction_ids=[str(i) for i in body.interaction_ids],
        reference_outputs=body.reference_outputs,
    )
    db.add(rs)
    await db.flush()
    return ReferenceSetOut(
        id=rs.id,
        tenant_id=rs.tenant_id,
        surface=rs.surface,
        name=rs.name,
        version=rs.version,
        interaction_ids=rs.interaction_ids,
        created_at=rs.created_at,
        frozen_at=rs.frozen_at,
    )


@router.post("/evaluation/reference-sets/{set_id}/freeze", response_model=ReferenceSetOut)
async def freeze_reference_set(
    set_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    rs = (
        await db.execute(
            select(EvaluationReferenceSet).where(
                EvaluationReferenceSet.id == set_id,
                EvaluationReferenceSet.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if rs is None:
        raise HTTPException(status_code=404, detail="Reference set not found")
    if rs.frozen_at is not None:
        raise HTTPException(status_code=409, detail="Already frozen")
    rs.frozen_at = datetime.utcnow()
    return ReferenceSetOut(
        id=rs.id,
        tenant_id=rs.tenant_id,
        surface=rs.surface,
        name=rs.name,
        version=rs.version,
        interaction_ids=rs.interaction_ids,
        created_at=rs.created_at,
        frozen_at=rs.frozen_at,
    )


@router.get("/evaluation/reference-sets", response_model=List[ReferenceSetOut])
async def list_reference_sets(
    surface: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(EvaluationReferenceSet).where(
        EvaluationReferenceSet.tenant_id == tenant.id
    )
    if surface:
        stmt = stmt.where(EvaluationReferenceSet.surface == surface)
    rows = (await db.execute(stmt.order_by(EvaluationReferenceSet.created_at.desc()))).scalars().all()
    return [
        ReferenceSetOut(
            id=r.id,
            tenant_id=r.tenant_id,
            surface=r.surface,
            name=r.name,
            version=r.version,
            interaction_ids=r.interaction_ids,
            created_at=r.created_at,
            frozen_at=r.frozen_at,
        )
        for r in rows
    ]


# ── Vocabulary candidates (Gate 1) ───────────────────────────────────────


class VocabCandidateOut(BaseModel):
    id: uuid.UUID
    term: str
    confidence: str
    source: Optional[str]
    occurrence_count: int
    status: str
    created_at: datetime


@router.get("/evaluation/vocabulary", response_model=List[VocabCandidateOut])
async def list_vocab_candidates(
    status: str = "pending",
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    rows = (
        await db.execute(
            select(VocabularyCandidate)
            .where(
                VocabularyCandidate.tenant_id == tenant.id,
                VocabularyCandidate.status == status,
            )
            .order_by(VocabularyCandidate.occurrence_count.desc())
        )
    ).scalars().all()
    return [
        VocabCandidateOut(
            id=r.id,
            term=r.term,
            confidence=r.confidence,
            source=r.source,
            occurrence_count=r.occurrence_count,
            status=r.status,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/evaluation/vocabulary/{cand_id}/approve", response_model=VocabCandidateOut)
async def approve_vocab(
    cand_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    return await _decide_vocab(cand_id, user_id, db, tenant, approve=True)


@router.post("/evaluation/vocabulary/{cand_id}/reject", response_model=VocabCandidateOut)
async def reject_vocab(
    cand_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    return await _decide_vocab(cand_id, user_id, db, tenant, approve=False)


async def _decide_vocab(
    cand_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
    db: AsyncSession,
    tenant: Tenant,
    *,
    approve: bool,
) -> VocabCandidateOut:
    """Apply approve/reject from the async API into the sync vocab service.

    The sync service does the cross-table writes (Tenant.keyterm_boost_list,
    TenantPromptConfig.custom_terms).  We bridge by running it in a sync
    session committed before returning.
    """
    cand = (
        await db.execute(
            select(VocabularyCandidate).where(
                VocabularyCandidate.id == cand_id,
                VocabularyCandidate.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Use a sync session so vocabulary_service can do the cross-row writes.
    from backend.app.services.vocabulary_service import (
        approve_candidate,
        reject_candidate,
    )
    from backend.app.tasks import _get_sync_session

    sync = _get_sync_session()
    try:
        sync_cand = sync.query(VocabularyCandidate).filter(
            VocabularyCandidate.id == cand_id
        ).first()
        if sync_cand is None:
            raise HTTPException(status_code=404, detail="Candidate not found")
        if approve:
            approve_candidate(sync, sync_cand, user_id)
        else:
            reject_candidate(sync, sync_cand, user_id)
        sync.commit()
    finally:
        sync.close()

    # Refresh in the async session.
    await db.refresh(cand)
    return VocabCandidateOut(
        id=cand.id,
        term=cand.term,
        confidence=cand.confidence,
        source=cand.source,
        occurrence_count=cand.occurrence_count,
        status=cand.status,
        created_at=cand.created_at,
    )
