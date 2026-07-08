"""Domain-agnostic trend engine: embed -> cluster -> growth -> confidence.

Extracted from ``support_trend_detector.py`` (originally IT_SUPPORT-only)
so Sales, CS, and cross-customer concern-aggregation callers can reuse the
same online-clustering + growth-detection + confidence-scoring pipeline
over their own corpora, instead of re-deriving the math per domain.

Nothing here is domain-specific: no SQLAlchemy models beyond the generic
``ManagerAlert`` write in ``persist_alerts`` (shared dedup plumbing every
caller needs), no LLM calls, no domain vocabulary. Each caller adapts its
own rows into ``TrendItem`` (text + timestamp + customer_id + source_id +
an already-computed embedding) and gets back ``EmergingTrend`` clusters
with the same growth/confidence semantics ``support_trend_detector``
already ships — see that module's docstring for the algorithm narrative
(online cosine clustering, 14d-vs-14d growth, confidence blend).

``support_trend_detector.py`` itself now calls into this module; its
externally observable behavior (constants, class/function names,
``EmergingTrend`` shape, persisted alerts/recommendations) is unchanged —
see its own module docstring and the no-regression tests in
``tests/test_support_trend_detector.py``.
"""
from __future__ import annotations

import hashlib
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── Tunables (identical defaults to the original support-only detector) ──

CLUSTER_SIM_THRESHOLD = 0.78
MIN_CLUSTER_SIZE = 3
GROWTH_WINDOW_DAYS = 14
GROWTH_RATIO = 2.0

# TTL for the shared Redis embedding cache used by callers that have no
# DB column to persist an embedding on (Sales/CS Interactions, cross-
# customer CustomerConcern rows). Matches ``support_trend_detector``'s
# ``EMBED_TTL_DAYS`` staleness budget for its persisted
# ``SupportCase.embedded_at`` column — same cost-bounding intent, just
# backed by the cache layer instead of a dedicated column.
CACHED_EMBED_TTL_DAYS = 30


# ── Corpus item + generic cluster ────────────────────────────────────────


@dataclass(frozen=True)
class TrendItem:
    """One corpus row going into the clustering pass.

    ``source_id`` and ``customer_id`` are opaque to the engine (every
    current caller uses UUIDs, but the engine never assumes that) — they
    ride along untouched so an ``EmergingTrend`` can be traced back to
    concrete rows. ``embedding`` must already be populated: which Voyage
    batch size / cache / TTL to use is a per-domain concern that stays in
    the caller (see ``build_cached_embedder`` / ``cluster_corpus`` below
    for the shared, no-DB-column path). ``weight`` defaults to 1.0 and
    lets a caller fold in a severity/valence reading (see
    ``EmergingTrend.total_weight``) without the engine knowing what the
    weight means.
    """

    source_id: Any
    text: str
    timestamp: Optional[datetime]
    customer_id: Optional[Any]
    embedding: Sequence[float]
    weight: float = 1.0


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class Cluster:
    """In-memory cluster while scanning. Centroid is the component-wise
    mean of the member vectors; updating it incrementally is fine at our
    N."""

    items: List[TrendItem] = field(default_factory=list)
    centroid: List[float] = field(default_factory=list)

    def add(self, item: TrendItem) -> None:
        n = len(self.items)
        vec = item.embedding
        if n == 0:
            self.centroid = list(vec)
        else:
            self.centroid = [
                (c * n + v) / (n + 1) for c, v in zip(self.centroid, vec)
            ]
        self.items.append(item)


def cluster_items(
    items: Sequence[TrendItem],
    *,
    threshold: float = CLUSTER_SIM_THRESHOLD,
) -> List[Cluster]:
    """Group items by embedding similarity. Online, single-pass.

    Order intentional: newest first, so cluster centroids reflect the
    current language pattern. An older item still matches if it's within
    threshold of a recent cluster's centroid. Identical algorithm to the
    original ``support_trend_detector.cluster_cases``.
    """
    embedded = [
        it
        for it in items
        if isinstance(it.embedding, (list, tuple)) and len(it.embedding)
    ]
    embedded.sort(key=lambda it: it.timestamp or datetime.min, reverse=True)
    clusters: List[Cluster] = []
    for it in embedded:
        best_idx = -1
        best_sim = 0.0
        for i, cl in enumerate(clusters):
            sim = cosine_similarity(it.embedding, cl.centroid)
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_idx >= 0 and best_sim >= threshold:
            clusters[best_idx].add(it)
        else:
            new_cl = Cluster()
            new_cl.add(it)
            clusters.append(new_cl)
    return clusters


# ── Emerging-trend rule ──────────────────────────────────────────────────


@dataclass(frozen=True)
class EmergingTrend:
    """One cluster that's currently growing.

    ``confidence`` aggregates cluster size + intra-cluster cohesion +
    growth ratio — see ``find_emerging_trends`` for the blend. This is
    the load-bearing number that differentiates "definitely fire" from
    "watch." ``total_weight`` sums each recent-window item's ``weight``
    (1.0 by default; a caller can fold in e.g. a severity reading) so a
    caller can distinguish "5 mild mentions" from "5 severe ones" at the
    same recent_count.
    """

    cluster_id: str
    recent_count: int
    prior_count: int
    growth_ratio: float
    confidence: float  # 0..1
    sample_texts: List[str]
    sample_ids: List[Any]
    customer_count: int
    total_weight: float = 0.0


def _ge(a: datetime, b: datetime) -> bool:
    """Naive-vs-aware-safe ``>=`` comparison."""
    if a.tzinfo is None and b.tzinfo is not None:
        return a >= b.replace(tzinfo=None)
    if a.tzinfo is not None and b.tzinfo is None:
        return a >= b.replace(tzinfo=b.tzinfo)  # noop, both aware
    return a >= b


def find_emerging_trends(
    clusters: Sequence[Cluster],
    *,
    now: Optional[datetime] = None,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
    growth_window_days: int = GROWTH_WINDOW_DAYS,
    growth_ratio: float = GROWTH_RATIO,
) -> List[EmergingTrend]:
    """Apply the growth-vs-prior rule + size floor.

    Cohesion proxy: average cosine similarity from cluster centroid to
    members. Higher = tighter cluster = lower false-positive risk.
    Identical rule to the original ``support_trend_detector.
    find_emerging_trends``, parameterized so a caller can tune the floor
    / window / ratio without forking the function.
    """
    now = now or datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=growth_window_days)
    prior_cutoff = now - timedelta(days=growth_window_days * 2)
    trends: List[EmergingTrend] = []
    for cl in clusters:
        if len(cl.items) < min_cluster_size:
            continue
        recent = [
            it
            for it in cl.items
            if it.timestamp is not None and _ge(it.timestamp, recent_cutoff)
        ]
        prior = [
            it
            for it in cl.items
            if it.timestamp is not None
            and _ge(it.timestamp, prior_cutoff)
            and not _ge(it.timestamp, recent_cutoff)
        ]
        if not recent or len(recent) < min_cluster_size:
            continue
        prior_count = max(len(prior), 1)
        growth = len(recent) / prior_count
        # If the prior window was empty AND recent is at floor, also
        # treat that as a real trend (fresh cluster forming).
        is_fresh = len(prior) == 0 and len(recent) >= min_cluster_size
        if growth < growth_ratio and not is_fresh:
            continue
        # Cohesion: mean similarity from centroid to members.
        if cl.items:
            sims = [
                cosine_similarity(it.embedding, cl.centroid) for it in cl.items
            ]
            cohesion = sum(sims) / len(sims) if sims else 0.0
        else:
            cohesion = 0.0
        # Confidence: weighted mix. Logistic-ish squashing on growth so a
        # 10x cluster doesn't dominate the same cluster on a 3x.
        growth_signal = min(growth / 4.0, 1.0)
        size_signal = min(len(recent) / 8.0, 1.0)
        confidence = 0.45 * cohesion + 0.35 * growth_signal + 0.20 * size_signal
        sample_texts = [it.text for it in cl.items[:5] if it.text]
        sample_ids = [it.source_id for it in cl.items[:5]]
        customer_set = {it.customer_id for it in cl.items if it.customer_id is not None}
        total_weight = sum(it.weight for it in recent)
        trends.append(
            EmergingTrend(
                cluster_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_OID, sample_texts[0] if sample_texts else "_"
                    )
                ),
                recent_count=len(recent),
                prior_count=len(prior),
                growth_ratio=round(growth, 2),
                confidence=round(min(max(confidence, 0.0), 1.0), 2),
                sample_texts=sample_texts,
                sample_ids=sample_ids,
                customer_count=len(customer_set),
                total_weight=round(total_weight, 2),
            )
        )
    return trends


# ── Shared embedder for callers with no persisted embedding column ───────


def build_cached_embedder() -> Any:
    """Voyage-backed (or whatever ``EMBEDDING_PROVIDER`` is configured)
    embedder for corpora that have no DB column to cache an embedding on
    (Sales/CS Interactions, cross-customer CustomerConcern rows) — unlike
    ``SupportCase.subject_embedding`` / ``embedded_at``. Backed by the
    existing Redis embedding cache (``embeddings.py``) with a
    ``CACHED_EMBED_TTL_DAYS`` TTL, which bounds Voyage cost the same way:
    a given piece of text is only re-embedded once per TTL window rather
    than once per scan.

    A fresh instance per call is intentional and cheap — the actual cache
    lives in Redis (keyed by provider + text), so a new
    ``EmbeddingService`` object still hits the same cached vectors; only
    the Redis-unavailable in-process fallback dict doesn't survive across
    calls, which just means a degraded-Redis run re-embeds instead of
    erroring.
    """
    from backend.app.services.embeddings import EmbeddingCache, EmbeddingService

    return EmbeddingService(cache=EmbeddingCache(ttl=CACHED_EMBED_TTL_DAYS * 24 * 3600))


async def cluster_corpus(
    rows: Sequence[Any],
    *,
    text_fn: Callable[[Any], Optional[str]],
    timestamp_fn: Callable[[Any], Optional[datetime]],
    customer_id_fn: Callable[[Any], Optional[Any]],
    source_id_fn: Callable[[Any], Any],
    weight_fn: Optional[Callable[[Any], float]] = None,
    embedder: Optional[Any] = None,
    threshold: float = CLUSTER_SIM_THRESHOLD,
) -> List[Cluster]:
    """Embed + cluster a domain corpus in one call.

    Each domain caller (sales trend, CS trend, concern aggregation) has
    its own row shape (``Interaction``, ``CustomerConcern``, ...); this
    turns rows into text via ``text_fn``, embeds whatever isn't skipped
    (``text_fn`` returning falsy skips the row), and clusters via
    ``cluster_items``. Rows are embedded in one batch — callers are
    expected to have already bounded the row count (lookback window).
    """
    embedder = embedder or build_cached_embedder()
    texts: List[str] = []
    kept_rows: List[Any] = []
    for row in rows:
        text = text_fn(row)
        if not text:
            continue
        texts.append(text)
        kept_rows.append(row)
    if not texts:
        return []
    vectors = await embedder.embed(texts)
    items = [
        TrendItem(
            source_id=source_id_fn(row),
            text=text,
            timestamp=timestamp_fn(row),
            customer_id=customer_id_fn(row),
            embedding=vec,
            weight=weight_fn(row) if weight_fn is not None else 1.0,
        )
        for row, text, vec in zip(kept_rows, texts, vectors)
    ]
    return cluster_items(items, threshold=threshold)


# ── Shared ManagerAlert persistence ──────────────────────────────────────


def build_alert_fingerprint(kind: str, cluster_id: str) -> str:
    return hashlib.sha256(f"{kind}::{cluster_id}".encode("utf-8")).hexdigest()[:32]


def persist_alerts(
    session: Session,
    tenant_id: Any,
    trends: Sequence[EmergingTrend],
    *,
    kind: str,
    domain: str,
    title_fn: Callable[[EmergingTrend], str],
    body_fn: Callable[[EmergingTrend], str],
    evidence_fn: Optional[Callable[[EmergingTrend], Dict[str, Any]]] = None,
) -> int:
    """Insert one ``ManagerAlert`` per still-open trend, deduped by
    ``fingerprint = sha256(kind::cluster_id)``.

    Mirrors the alert half of ``support_trend_detector.persist_trends``
    so every domain caller (sales, CS, cross-customer concerns) gets
    identical dedup semantics without re-deriving the hashing. Flushes
    but does not commit — the caller commits once after also persisting
    any recommendations for the same run.
    """
    from backend.app.models import ManagerAlert

    inserted = 0
    for t in trends:
        fingerprint = build_alert_fingerprint(kind, t.cluster_id)
        existing = session.execute(
            select(ManagerAlert.id).where(
                ManagerAlert.tenant_id == tenant_id,
                ManagerAlert.fingerprint == fingerprint,
                ManagerAlert.resolved_at.is_(None),
            )
        ).first()
        if existing is not None:
            continue
        severity = "high" if t.confidence >= 0.7 else "medium"
        evidence = (
            evidence_fn(t)
            if evidence_fn is not None
            else {
                "recent_count": t.recent_count,
                "prior_count": t.prior_count,
                "growth_ratio": t.growth_ratio,
                "confidence": t.confidence,
                "customer_count": t.customer_count,
                "sample_texts": t.sample_texts[:3],
            }
        )
        alert = ManagerAlert(
            tenant_id=tenant_id,
            kind=kind,
            severity=severity,
            title=title_fn(t)[:300],
            body=body_fn(t),
            evidence=evidence,
            fingerprint=fingerprint,
            domain=domain,
        )
        session.add(alert)
        inserted += 1
    if inserted:
        session.flush()
    return inserted
