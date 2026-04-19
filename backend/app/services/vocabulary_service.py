"""Discover vocabulary candidates from corrections + low-confidence segments.

Sources, in order of confidence:
- transcript_corrections — when the same word is corrected ≥ 3 times within a
  tenant we treat the corrected form as a high-confidence candidate.
- transcript segments with confidence < 0.85 — we extract proper nouns and
  multi-word phrases as medium-confidence candidates.
- action_item titles containing words not in the tenant's current vocabulary
  — low-confidence candidates.

Approved candidates sync into ``Tenant.keyterm_boost_list`` and into
``TenantPromptConfig.custom_terms``.  The approval flow itself is exposed via
the analytics + admin endpoints — see ``api/analytics.py`` and
``api/evaluation.py``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Dict, Iterable, List

from sqlalchemy.orm import Session

from backend.app.models import (
    ActionItem,
    Interaction,
    Tenant,
    TranscriptCorrection,
    VocabularyCandidate,
)

logger = logging.getLogger(__name__)

CORRECTION_FREQ_HIGH = 3   # ≥ this many corrections of same word → high
LOW_CONFIDENCE_CUTOFF = 0.85
PROPER_NOUN_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{2,}(?:\s+[A-Z][A-Za-z0-9]+)?\b")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def _extract_corrected_terms(corrections: Iterable[TranscriptCorrection]) -> Counter:
    counts: Counter = Counter()
    for c in corrections:
        # Tokens that appear in the corrected text but not in the original
        # are the highest-signal candidates — those are the words ASR missed.
        orig_tokens = set(t.lower() for t in WORD_RE.findall(c.original_text or ""))
        corr_tokens = WORD_RE.findall(c.corrected_text or "")
        for tok in corr_tokens:
            if tok.lower() not in orig_tokens and len(tok) > 3:
                counts[tok] += 1
    return counts


def _extract_low_confidence_proper_nouns(
    segments_iter: Iterable[Dict[str, Any]],
) -> Counter:
    counts: Counter = Counter()
    for seg in segments_iter:
        try:
            confidence = float(seg.get("confidence") or 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        if confidence > LOW_CONFIDENCE_CUTOFF:
            continue
        for match in PROPER_NOUN_RE.findall(seg.get("text") or ""):
            if 3 < len(match) <= 50:
                counts[match] += 1
    return counts


def _extract_action_item_terms(
    items: Iterable[ActionItem], existing_terms: List[str]
) -> Counter:
    existing = {t.lower() for t in existing_terms}
    counts: Counter = Counter()
    for item in items:
        for tok in WORD_RE.findall(((item.title or "") + " " + (item.description or ""))):
            if len(tok) > 3 and tok.lower() not in existing and tok[0].isupper():
                counts[tok] += 1
    return counts


def _upsert_candidate(
    session: Session, tenant_id: Any, term: str, source: str, confidence: str, count: int
) -> None:
    """Insert or update a candidate (unique on tenant_id, term)."""
    existing = (
        session.query(VocabularyCandidate)
        .filter(
            VocabularyCandidate.tenant_id == tenant_id,
            VocabularyCandidate.term == term,
        )
        .first()
    )
    if existing is None:
        session.add(
            VocabularyCandidate(
                tenant_id=tenant_id,
                term=term,
                source=source,
                confidence=confidence,
                occurrence_count=count,
            )
        )
        return
    if existing.status != "pending":
        return  # respect prior approval/rejection
    existing.occurrence_count = (existing.occurrence_count or 0) + count
    # Upgrade confidence if the new sighting is from a stronger source.
    rank = {"low": 0, "medium": 1, "high": 2}
    if rank.get(confidence, 0) > rank.get(existing.confidence, 0):
        existing.confidence = confidence
    if existing.occurrence_count >= CORRECTION_FREQ_HIGH and source == "corrections":
        existing.confidence = "high"


def _discover_for_tenant(session: Session, tenant: Tenant) -> int:
    # 1. Corrections within last 30 days.
    corrections = (
        session.query(TranscriptCorrection)
        .filter(TranscriptCorrection.tenant_id == tenant.id)
        .all()
    )
    correction_counts = _extract_corrected_terms(corrections)
    for term, count in correction_counts.items():
        confidence = "high" if count >= CORRECTION_FREQ_HIGH else "medium"
        _upsert_candidate(session, tenant.id, term, "corrections", confidence, count)

    # 2. Low-confidence proper nouns from interactions.
    interactions = (
        session.query(Interaction)
        .filter(Interaction.tenant_id == tenant.id)
        .order_by(Interaction.created_at.desc())
        .limit(200)
        .all()
    )
    low_conf_counts: Counter = Counter()
    for interaction in interactions:
        low_conf_counts.update(
            _extract_low_confidence_proper_nouns(interaction.transcript or [])
        )
    for term, count in low_conf_counts.items():
        if count >= 2:  # noise floor
            _upsert_candidate(
                session, tenant.id, term, "low_confidence_segments", "medium", count
            )

    # 3. Action-item titles not in the existing vocabulary.
    items = (
        session.query(ActionItem)
        .filter(ActionItem.tenant_id == tenant.id)
        .order_by(ActionItem.created_at.desc())
        .limit(500)
        .all()
    )
    existing_terms = list(tenant.keyterm_boost_list or [])
    action_counts = _extract_action_item_terms(items, existing_terms)
    for term, count in action_counts.items():
        _upsert_candidate(session, tenant.id, term, "action_items", "low", count)

    return (
        len(correction_counts) + len(low_conf_counts) + len(action_counts)
    )


def discover_candidates_all_tenants(session: Session) -> Dict[str, Any]:
    tenants = session.query(Tenant).all()
    total = 0
    for tenant in tenants:
        try:
            total += _discover_for_tenant(session, tenant)
        except Exception:
            logger.exception("Vocabulary discovery failed for tenant %s", tenant.id)
    session.commit()
    return {"tenants_processed": len(tenants), "candidates_inspected": total}


# ── Approval flow ────────────────────────────────────────────────────────


def approve_candidate(session: Session, candidate: VocabularyCandidate, user_id: Any) -> None:
    """Approve and propagate to Tenant.keyterm_boost_list + TenantPromptConfig.custom_terms."""
    candidate.status = "approved"
    candidate.reviewed_by = user_id

    tenant = session.query(Tenant).filter(Tenant.id == candidate.tenant_id).first()
    if tenant is None:
        return
    boost = list(tenant.keyterm_boost_list or [])
    if candidate.term not in boost:
        boost.append(candidate.term)
        tenant.keyterm_boost_list = boost

    from backend.app.services.personalization_service import _get_or_create_config

    config = _get_or_create_config(session, tenant.id)
    custom_terms = list(config.custom_terms or [])
    if candidate.term not in custom_terms:
        custom_terms.append(candidate.term)
        config.custom_terms = custom_terms


def reject_candidate(session: Session, candidate: VocabularyCandidate, user_id: Any) -> None:
    candidate.status = "rejected"
    candidate.reviewed_by = user_id
