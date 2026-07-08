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

As of the trend-engine extraction, the actual clustering / growth /
confidence math lives in ``trend_engine.py`` (domain-agnostic; also used
by the Sales and CS trend callers and cross-customer concern
aggregation). This module is now a thin SupportCase-shaped adapter over
that engine: ``cluster_cases`` and ``find_emerging_trends`` convert
``SupportCase`` rows to/from the engine's generic ``TrendItem`` /
``Cluster`` / ``EmergingTrend`` shapes so every name this module exposed
before (``_cosine_similarity``, ``_Cluster``, ``CLUSTER_SIM_THRESHOLD``,
``cluster_cases``, ``find_emerging_trends``, ``EmergingTrend``,
``persist_trends``) keeps its exact prior signature and behavior — see
``tests/test_support_trend_detector.py``.
"""
from __future__ import annotations

import logging
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
from backend.app.services import trend_engine

logger = logging.getLogger(__name__)


# ── Tunables ───────────────────────────────────────────────────────────
# Same values as ``trend_engine``'s defaults — re-bound here (rather than
# imported under a different name) so this module's public constants are
# unchanged for any existing caller/test.

# Cosine similarity floor for joining an existing cluster. Voyage-2
# embeddings on short subjects cluster well around 0.78–0.82; pick
# the conservative end to keep false positives low.
CLUSTER_SIM_THRESHOLD = trend_engine.CLUSTER_SIM_THRESHOLD

# Floor on cluster size before we even consider firing. The product
# direction says hard counts are a FLOOR, not a trigger; this is the
# floor.
MIN_CLUSTER_SIZE = trend_engine.MIN_CLUSTER_SIZE

# Trailing window vs prior window. Both windows are this many days.
GROWTH_WINDOW_DAYS = trend_engine.GROWTH_WINDOW_DAYS

# Multiplier on the prior window's count that the trailing window must
# exceed to fire. 2.0 = doubled.
GROWTH_RATIO = trend_engine.GROWTH_RATIO

# How many cases to embed in one Voyage call. Voyage's batch endpoint
# tops out around 128; pick a number that keeps the per-call payload
# under their 10 MB limit and the latency under 5 s.
EMBED_BATCH_SIZE = 64


@dataclass
class _Cluster:
    """In-memory cluster while we're scanning. Centroid is the
    component-wise mean of the member vectors; updating it
    incrementally is fine at our N.

    Kept as a SupportCase-shaped wrapper (``.cases``, not the engine's
    generic ``.items``) so existing callers/tests are untouched; built
    from / converted to ``trend_engine.Cluster`` internally.
    """

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

# Re-exported so existing callers/tests importing ``_cosine_similarity``
# from this module keep working unchanged.
_cosine_similarity = trend_engine.cosine_similarity


def _case_to_item(case: SupportCase) -> trend_engine.TrendItem:
    return trend_engine.TrendItem(
        source_id=case.id,
        text=case.subject,
        timestamp=case.opened_at,
        customer_id=case.customer_id,
        embedding=case.subject_embedding,
    )


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

    Delegates the actual clustering to ``trend_engine.cluster_items``;
    this just adapts ``SupportCase`` rows to/from the engine's generic
    ``TrendItem`` / ``Cluster`` shapes so callers keep seeing ``_Cluster``
    objects with a ``.cases`` list of ``SupportCase``.
    """
    candidates = [
        c for c in cases if isinstance(c.subject_embedding, list) and c.subject_embedding
    ]
    by_id = {c.id: c for c in candidates}
    items = [_case_to_item(c) for c in candidates]
    generic_clusters = trend_engine.cluster_items(items, threshold=CLUSTER_SIM_THRESHOLD)
    return [
        _Cluster(
            cases=[by_id[it.source_id] for it in gc.items],
            centroid=gc.centroid,
        )
        for gc in generic_clusters
    ]


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

    Delegates to ``trend_engine.find_emerging_trends``; converts
    ``_Cluster`` (SupportCase-shaped) to the engine's generic ``Cluster``
    on the way in, and maps the engine's generic ``EmergingTrend`` back
    onto this module's ``EmergingTrend`` (``sample_subjects`` /
    ``sample_case_ids`` instead of ``sample_texts`` / ``sample_ids``) on
    the way out, so nothing about this module's public shape changes.
    """
    generic_clusters = [
        trend_engine.Cluster(
            items=[_case_to_item(c) for c in cl.cases],
            centroid=cl.centroid,
        )
        for cl in clusters
    ]
    generic_trends = trend_engine.find_emerging_trends(
        generic_clusters,
        now=now,
        min_cluster_size=MIN_CLUSTER_SIZE,
        growth_window_days=GROWTH_WINDOW_DAYS,
        growth_ratio=GROWTH_RATIO,
    )
    return [
        EmergingTrend(
            cluster_id=gt.cluster_id,
            recent_count=gt.recent_count,
            prior_count=gt.prior_count,
            growth_ratio=gt.growth_ratio,
            confidence=gt.confidence,
            sample_subjects=gt.sample_texts,
            sample_case_ids=gt.sample_ids,
            customer_count=gt.customer_count,
        )
        for gt in generic_trends
    ]


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
                    f"Recurring issue: {t.sample_subjects[0][:80]}"
                    if t.sample_subjects
                    else "Recurring issue across cases"
                ),
                body=(
                    f"{t.recent_count} similar cases in the last two "
                    f"weeks, up from {t.prior_count} the two weeks before. "
                    f"{t.customer_count} customer"
                    f"{'s' if t.customer_count != 1 else ''} affected."
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
                    f"Investigate recurring issue: {t.sample_subjects[0][:80]}"
                    if t.sample_subjects
                    else "Investigate recurring issue across cases"
                ),
                rationale=(
                    f"{t.recent_count} similar cases in the last two "
                    f"weeks from {t.customer_count} customer"
                    f"{'s' if t.customer_count != 1 else ''}. "
                    "Worth digging into the root cause."
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
