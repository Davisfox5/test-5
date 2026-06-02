"""Cross-customer recurring-issue detector for Support cases.

Per the v2 product direction: AI-driven, not threshold-driven. The
hard count of "N cases in M days" is the floor (we never fire on a
single case), not the trigger. The actual trigger is **growth** —
a cluster of similar cases where the recent share is larger than the
historical baseline. A cluster that's been stable at 4 cases / month
for the last 6 months doesn't fire; one that just doubled does.

Pipeline:

1. Embed any SupportCase that's missing ``subject_embedding`` and
   whose subject is non-empty. Done in batches per tenant.
2. Cluster cases per tenant using an online cosine-similarity
   threshold (``CLUSTER_SIM_THRESHOLD``). The algorithm is
   intentionally simple — sort cases newest-first, scan, attach each
   to the first existing cluster whose centroid scores >= threshold,
   otherwise start a new cluster.
3. For each cluster with >= ``MIN_CLUSTER_SIZE`` cases, compute a
   recency-weighted growth signal: cases in the trailing 14d vs
   cases in the prior 14d. Fire on a doubling or more (configurable
   via ``GROWTH_RATIO``).
4. Persist a ``ManagerAlert`` with ``kind=recurring_issue_detected``
   and a ``ManagerRecommendation`` of category
   ``address_recurring_issue`` so a Support manager can act on the
   trend from the existing portal.

Cluster centroids are not persisted — the run is fast enough that
re-clustering every night is cheaper than maintaining state.
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import (
    ManagerAlert,
    ManagerRecommendation,
    SupportCase,
    Tenant,
)

logger = logging.getLogger(__name__)


# ── Tunables ───────────────────────────────────────────────────────────

# Cosine similarity floor for joining an existing cluster. Voyage-2
# embeddings on short subjects cluster well around 0.78–0.82; pick
# the conservative end to keep false positives low.
CLUSTER_SIM_THRESHOLD = 0.78

# Floor on cluster size before we even consider firing. The product
# direction says hard counts are a FLOOR, not a trigger; this is the
# floor.
MIN_CLUSTER_SIZE = 3

# Trailing window vs prior window. Both windows are this many days.
GROWTH_WINDOW_DAYS = 14

# Multiplier on the prior window's count that the trailing window must
# exceed to fire. 2.0 = doubled.
GROWTH_RATIO = 2.0

# How many cases to embed in one Voyage call. Voyage's batch endpoint
# tops out around 128; pick a number that keeps the per-call payload
# under their 10 MB limit and the latency under 5 s.
EMBED_BATCH_SIZE = 64


@dataclass
class _Cluster:
    """In-memory cluster while we're scanning. Centroid is the
    component-wise mean of the member vectors; updating it
    incrementally is fine at our N."""

    cases: List[SupportCase] = field(default_factory=list)
    centroid: List[float] = field(default_factory=list)

    def add(self, case: SupportCase, vec: Sequence[float]) -> None:
        n = len(self.cases)
        if n == 0:
            self.centroid = list(vec)
        else:
            self.centroid = [
                (c * n + v) / (n + 1) for c, v in zip(self.centroid, vec)
            ]
        self.cases.append(case)


# ── Math helpers ─────────────────────────────────────────────────────


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── Embedding pass ───────────────────────────────────────────────────


EMBED_TTL_DAYS = 30


async def embed_missing_subjects(
    session: Session, tenant: Tenant
) -> int:
    """Embed any case whose ``subject_embedding`` is missing or stale.

    A case qualifies if (a) it has no embedding yet, or (b) the
    embedding is older than ``EMBED_TTL_DAYS`` — case subjects evolve
    over time (rep updates the title, the underlying issue changes
    flavor) and stale embeddings drift the cluster centroids. The TTL
    forces a refresh so trends fire on current language.

    Best-effort: Voyage failures land in the logger; the cluster pass
    skips cases without an embedding so the run still progresses.
    Returns the number of cases embedded.
    """
    from sqlalchemy import or_

    ttl_cutoff = datetime.now(timezone.utc) - timedelta(days=EMBED_TTL_DAYS)
    cases = (
        session.execute(
            select(SupportCase).where(
                SupportCase.tenant_id == tenant.id,
                SupportCase.subject.is_not(None),
                or_(
                    SupportCase.subject_embedding.is_(None),
                    SupportCase.embedded_at.is_(None),
                    SupportCase.embedded_at < ttl_cutoff,
                ),
            )
        ).scalars().all()
    )
    if not cases:
        return 0
    try:
        from backend.app.services.embeddings import VoyageEmbedder
    except Exception:
        logger.exception("VoyageEmbedder unavailable; skipping embed pass")
        return 0
    embedder = VoyageEmbedder()
    n = 0
    for i in range(0, len(cases), EMBED_BATCH_SIZE):
        batch = cases[i : i + EMBED_BATCH_SIZE]
        texts = [c.subject for c in batch]
        try:
            vecs = await embedder.embed(texts, input_type="document")
        except Exception:
            logger.exception(
                "VoyageEmbedder batch failed for tenant %s (skipping batch)",
                tenant.id,
            )
            continue
        for c, vec in zip(batch, vecs):
            c.subject_embedding = list(vec)
            c.embedded_at = datetime.now(timezone.utc)
            n += 1
        session.flush()
    if n:
        session.commit()
    return n


# ── Cluster + decide ─────────────────────────────────────────────────


def cluster_cases(
    cases: Sequence[SupportCase],
) -> List[_Cluster]:
    """Group cases by embedding similarity. Online, single-pass.

    Order intentional: newest first, so cluster centroids reflect the
    current language pattern. An older case still matches if it's
    within threshold of a recent cluster's centroid.
    """
    embedded = [
        c for c in cases if isinstance(c.subject_embedding, list) and c.subject_embedding
    ]
    embedded.sort(key=lambda c: c.opened_at or datetime.min, reverse=True)
    clusters: List[_Cluster] = []
    for c in embedded:
        vec = c.subject_embedding
        best_idx = -1
        best_sim = 0.0
        for i, cl in enumerate(clusters):
            sim = _cosine_similarity(vec, cl.centroid)
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_idx >= 0 and best_sim >= CLUSTER_SIM_THRESHOLD:
            clusters[best_idx].add(c, vec)
        else:
            new_cl = _Cluster()
            new_cl.add(c, vec)
            clusters.append(new_cl)
    return clusters


@dataclass(frozen=True)
class EmergingTrend:
    """One cluster that's currently growing.

    ``confidence`` aggregates cluster size + intra-cluster cohesion +
    growth ratio. A trend that's bigger, tighter, and growing faster
    gets a higher score; this is the load-bearing number that
    differentiates "definitely fire" from "watch."
    """

    cluster_id: str
    recent_count: int
    prior_count: int
    growth_ratio: float
    confidence: float  # 0..1
    sample_subjects: List[str]
    sample_case_ids: List[uuid.UUID]
    customer_count: int


def find_emerging_trends(
    clusters: Sequence[_Cluster],
    *,
    now: Optional[datetime] = None,
) -> List[EmergingTrend]:
    """Apply the growth-vs-prior rule + size floor.

    Cohesion proxy: average cosine similarity from cluster centroid to
    members. Higher = tighter cluster = lower false-positive risk.
    """
    now = now or datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=GROWTH_WINDOW_DAYS)
    prior_cutoff = now - timedelta(days=GROWTH_WINDOW_DAYS * 2)
    trends: List[EmergingTrend] = []
    for cl in clusters:
        if len(cl.cases) < MIN_CLUSTER_SIZE:
            continue
        recent = [
            c
            for c in cl.cases
            if c.opened_at is not None
            and _ge(c.opened_at, recent_cutoff)
        ]
        prior = [
            c
            for c in cl.cases
            if c.opened_at is not None
            and _ge(c.opened_at, prior_cutoff)
            and not _ge(c.opened_at, recent_cutoff)
        ]
        if not recent or len(recent) < MIN_CLUSTER_SIZE:
            continue
        prior_count = max(len(prior), 1)
        growth = len(recent) / prior_count
        # If the prior window was empty AND recent is at floor, also
        # treat that as a real trend (fresh cluster forming).
        is_fresh = len(prior) == 0 and len(recent) >= MIN_CLUSTER_SIZE
        if growth < GROWTH_RATIO and not is_fresh:
            continue
        # Cohesion: mean similarity from centroid to members.
        if cl.cases:
            sims = [
                _cosine_similarity(c.subject_embedding, cl.centroid)
                for c in cl.cases
            ]
            cohesion = sum(sims) / len(sims) if sims else 0.0
        else:
            cohesion = 0.0
        # Confidence: weighted mix. Logistic-ish squashing on growth
        # so a 10x cluster doesn't dominate the same cluster on a 3x.
        growth_signal = min(growth / 4.0, 1.0)
        size_signal = min(len(recent) / 8.0, 1.0)
        confidence = 0.45 * cohesion + 0.35 * growth_signal + 0.20 * size_signal
        sample_subjects = [c.subject for c in cl.cases[:5] if c.subject]
        sample_ids = [c.id for c in cl.cases[:5]]
        customer_set = {c.customer_id for c in cl.cases if c.customer_id is not None}
        trends.append(
            EmergingTrend(
                cluster_id=str(uuid.uuid5(uuid.NAMESPACE_OID, sample_subjects[0] if sample_subjects else "_")),
                recent_count=len(recent),
                prior_count=len(prior),
                growth_ratio=round(growth, 2),
                confidence=round(min(max(confidence, 0.0), 1.0), 2),
                sample_subjects=sample_subjects,
                sample_case_ids=sample_ids,
                customer_count=len(customer_set),
            )
        )
    return trends


def _ge(a: datetime, b: datetime) -> bool:
    """Naive-vs-aware-safe ``>=`` comparison."""
    if a.tzinfo is None and b.tzinfo is not None:
        return a >= b.replace(tzinfo=None)
    if a.tzinfo is not None and b.tzinfo is None:
        return a >= b.replace(tzinfo=b.tzinfo)  # noop, both aware
    return a >= b


# ── Persistence: alert + recommendation ──────────────────────────────


def persist_trends(
    session: Session,
    tenant: Tenant,
    trends: Sequence[EmergingTrend],
) -> Dict[str, int]:
    """Write a ManagerAlert + ManagerRecommendation per trend.

    Dedup by fingerprint (``recurring_issue:<cluster_id>``) so a
    cluster that re-fires across multiple nights doesn't fan out.
    """
    import hashlib

    alerts_inserted = 0
    recs_inserted = 0
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=14)
    for t in trends:
        fingerprint = hashlib.sha256(
            f"recurring_issue::{t.cluster_id}".encode("utf-8")
        ).hexdigest()[:32]
        # Alert dedup.
        existing_alert = session.execute(
            select(ManagerAlert.id).where(
                ManagerAlert.tenant_id == tenant.id,
                ManagerAlert.fingerprint == fingerprint,
                ManagerAlert.resolved_at.is_(None),
            )
        ).first()
        severity = "high" if t.confidence >= 0.7 else "medium"
        if existing_alert is None:
            alert = ManagerAlert(
                tenant_id=tenant.id,
                kind="recurring_issue_detected",
                severity=severity,
                title=(
                    f"Possible recurring issue: {t.sample_subjects[0][:80]}"
                    if t.sample_subjects
                    else "Possible recurring issue across cases"
                ),
                body=(
                    f"{t.recent_count} similar cases in the last "
                    f"{GROWTH_WINDOW_DAYS} days (vs {t.prior_count} in the "
                    f"prior window). Growth ratio {t.growth_ratio:.1f}x, "
                    f"confidence {t.confidence:.0%}."
                ),
                evidence={
                    "recent_count": t.recent_count,
                    "prior_count": t.prior_count,
                    "growth_ratio": t.growth_ratio,
                    "confidence": t.confidence,
                    "customer_count": t.customer_count,
                    "sample_subjects": t.sample_subjects[:3],
                    "sample_case_ids": [str(i) for i in t.sample_case_ids[:5]],
                },
                fingerprint=fingerprint,
                domain="it_support",
            )
            session.add(alert)
            alerts_inserted += 1
        # Recommendation dedup. We can't portably issue a JSONB
        # ``contains`` query against the ``target`` column (Postgres
        # uses ``@>``; SQLite test bind has no equivalent). Instead,
        # fetch open/applied rows of this category and filter the
        # cluster_id match in Python — at the volumes we expect
        # (single-digit recommendations per category per tenant)
        # this is cheap.
        candidates = (
            session.execute(
                select(ManagerRecommendation).where(
                    ManagerRecommendation.tenant_id == tenant.id,
                    ManagerRecommendation.category
                    == "address_recurring_issue",
                    ManagerRecommendation.status.in_(("open", "applied")),
                )
            )
        ).scalars().all()
        existing_rec = next(
            (
                r
                for r in candidates
                if isinstance(r.target, dict)
                and r.target.get("cluster_id") == t.cluster_id
            ),
            None,
        )
        if existing_rec is None:
            rec = ManagerRecommendation(
                tenant_id=tenant.id,
                domain="it_support",
                category="address_recurring_issue",
                title=(
                    f"Address recurring issue: {t.sample_subjects[0][:80]}"
                    if t.sample_subjects
                    else "Address recurring issue across cases"
                ),
                rationale=(
                    f"{t.recent_count} similar cases in the last "
                    f"{GROWTH_WINDOW_DAYS} days from {t.customer_count} "
                    f"customer(s). Growth {t.growth_ratio:.1f}x; confidence "
                    f"{t.confidence:.0%}. Likely root-cause investigation "
                    "candidate."
                ),
                evidence={
                    "recent_count": t.recent_count,
                    "prior_count": t.prior_count,
                    "growth_ratio": t.growth_ratio,
                    "confidence": t.confidence,
                    "customer_count": t.customer_count,
                    "sample_case_ids": [str(i) for i in t.sample_case_ids[:5]],
                    "sample_subjects": t.sample_subjects[:3],
                },
                target={
                    "cluster_id": t.cluster_id,
                    "sample_case_id": str(t.sample_case_ids[0])
                    if t.sample_case_ids
                    else None,
                },
                score=int(t.confidence * 100),
                expires_at=expires_at,
            )
            session.add(rec)
            recs_inserted += 1
    if alerts_inserted or recs_inserted:
        session.commit()
    return {
        "alerts_inserted": alerts_inserted,
        "recs_inserted": recs_inserted,
    }


async def run_for_tenant(session: Session, tenant: Tenant) -> Dict[str, Any]:
    """End-to-end run: embed any missing, cluster, find trends, persist.

    The embedding pass is async (Voyage call); the rest is sync.
    """
    embedded = await embed_missing_subjects(session, tenant)
    # Re-fetch with embeddings populated.
    cases = (
        session.execute(
            select(SupportCase).where(
                SupportCase.tenant_id == tenant.id,
                SupportCase.subject_embedding.isnot(None),
            )
        ).scalars().all()
    )
    clusters = cluster_cases(cases)
    trends = find_emerging_trends(clusters)
    persisted = persist_trends(session, tenant, trends)
    return {
        "embedded": embedded,
        "clusters": len(clusters),
        "trends_found": len(trends),
        **persisted,
    }
