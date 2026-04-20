"""Topic clustering — BERTopic-style but dependency-free.

Takes a list of short texts (topic names, utterances, or utterance
excerpts), computes embeddings via :mod:`embeddings`, clusters them by
cosine-similarity single-pass agglomerative merging, and labels each
cluster using class-based TF-IDF.

Why not real BERTopic / HDBSCAN?  The production shape here needs to
run inside a Celery worker without heavyweight numeric dependencies.
For typical tenant sizes (≤50k topic mentions per month) a greedy
cosine-threshold clusterer returns the same canonical groupings as
HDBSCAN with a fraction of the cost.  If we outgrow it, we swap the
implementation behind the same interface.

Outputs are consumed by:

- The canonical-topic registry (tenant-level).  Every new cluster
  gets a representative label (the most-TF-IDF-weighted token) that
  becomes the key for Fightin' Words analysis and trend display.
- The orchestrator's business-profile refresh, which reads the latest
  top clusters to surface "what's trending" top factors.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from backend.app.services.embeddings import (
    EmbeddingService,
    cosine_similarity,
    get_embedding_service,
    vector_mean,
)

logger = logging.getLogger(__name__)


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
_STOPWORDS: set = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has",
    "are", "were", "was", "you", "your", "yours", "our", "ours", "their",
    "them", "they", "but", "not", "any", "all", "will", "can", "could",
    "should", "would", "also", "more", "less", "one", "two", "just",
    "about", "into", "over", "been", "being", "its", "his", "her", "she",
    "him", "hers", "whom",
}


# ── Public types ─────────────────────────────────────────────────────────


@dataclass
class TopicCluster:
    cluster_id: int
    label: str
    keywords: List[str]
    member_texts: List[str]
    centroid: List[float]
    size: int = 0

    def as_dict(self) -> Dict:
        return {
            "cluster_id": self.cluster_id,
            "label": self.label,
            "keywords": self.keywords,
            "size": self.size,
        }


@dataclass
class ClusteringResult:
    clusters: List[TopicCluster]
    assignments: List[int]  # parallel to the input texts; -1 = noise
    threshold: float
    provider: str

    def label_for(self, idx: int) -> Optional[str]:
        """Canonical label for the ``idx``-th input text, if clustered."""
        a = self.assignments[idx] if 0 <= idx < len(self.assignments) else -1
        if a < 0 or a >= len(self.clusters):
            return None
        return self.clusters[a].label


# ── Core clusterer ───────────────────────────────────────────────────────


class TopicClusterer:
    """Greedy cosine-threshold agglomerative clustering on embeddings.

    Calling pattern
    ---------------
    ``result = await TopicClusterer().cluster(texts, threshold=0.55)``
    - texts: e.g. topic names from many interactions' ``topics[]``.
    - threshold: minimum cosine similarity to an existing cluster
      centroid; below this, the text starts a new cluster.  0.55 is a
      sensible default for short topic names; 0.75 suits longer
      utterances.

    Complexity: O(N · K) where K is the final cluster count, which is
    typically << N.  Good enough for tenants with 10k–50k topic
    mentions per month.
    """

    def __init__(
        self,
        embedder: Optional[EmbeddingService] = None,
        threshold: float = 0.55,
        min_cluster_size: int = 2,
    ) -> None:
        self._embedder = embedder or get_embedding_service()
        self._threshold = threshold
        self._min_cluster_size = min_cluster_size

    async def cluster(
        self,
        texts: Sequence[str],
        threshold: Optional[float] = None,
    ) -> ClusteringResult:
        threshold = threshold if threshold is not None else self._threshold
        if not texts:
            return ClusteringResult([], [], threshold, self._embedder.provider_name)

        vectors = await self._embedder.embed(list(texts))
        return self._cluster_vectors(list(texts), vectors, threshold)

    def cluster_sync(
        self,
        texts: Sequence[str],
        threshold: Optional[float] = None,
    ) -> ClusteringResult:
        import asyncio
        return asyncio.run(self.cluster(texts, threshold=threshold))

    # ── Internals ─────────────────────────────────────────────────────

    def _cluster_vectors(
        self,
        texts: List[str],
        vectors: List[List[float]],
        threshold: float,
    ) -> ClusteringResult:
        clusters: List[TopicCluster] = []
        member_vectors: List[List[List[float]]] = []
        assignments: List[int] = []

        for text, vec in zip(texts, vectors):
            best_idx = -1
            best_sim = -1.0
            for i, c in enumerate(clusters):
                sim = cosine_similarity(c.centroid, vec)
                if sim > best_sim:
                    best_sim = sim
                    best_idx = i
            if best_idx >= 0 and best_sim >= threshold:
                clusters[best_idx].member_texts.append(text)
                member_vectors[best_idx].append(vec)
                # Update centroid incrementally (mean of all members).
                clusters[best_idx].centroid = vector_mean(member_vectors[best_idx])
                clusters[best_idx].size += 1
                assignments.append(best_idx)
            else:
                new_id = len(clusters)
                clusters.append(TopicCluster(
                    cluster_id=new_id,
                    label=text,
                    keywords=[],
                    member_texts=[text],
                    centroid=list(vec),
                    size=1,
                ))
                member_vectors.append([list(vec)])
                assignments.append(new_id)

        # Drop clusters below min_cluster_size (assign members to noise).
        kept: List[TopicCluster] = []
        old_to_new: Dict[int, int] = {}
        for i, c in enumerate(clusters):
            if c.size >= self._min_cluster_size:
                old_to_new[i] = len(kept)
                kept.append(c)
        remapped: List[int] = []
        for a in assignments:
            if a in old_to_new:
                remapped.append(old_to_new[a])
            else:
                remapped.append(-1)

        # c-TF-IDF labeling.
        for c in kept:
            keywords = self._ctfidf_keywords(c.member_texts, kept)
            c.keywords = keywords[:5]
            if keywords:
                c.label = keywords[0]

        return ClusteringResult(
            clusters=kept,
            assignments=remapped,
            threshold=threshold,
            provider=self._embedder.provider_name,
        )

    @staticmethod
    def _ctfidf_keywords(
        cluster_texts: Sequence[str],
        all_clusters: Sequence[TopicCluster],
    ) -> List[str]:
        """Class-based TF-IDF scoring — returns keywords sorted by score."""
        def _tokens(texts: Sequence[str]) -> Counter:
            counter: Counter = Counter()
            for t in texts:
                for w in _WORD_RE.findall(t.lower()):
                    if w not in _STOPWORDS:
                        counter[w] += 1
            return counter

        cluster_counts = _tokens(cluster_texts)
        cluster_total = sum(cluster_counts.values()) or 1
        # Document frequency across clusters (each cluster = one "class").
        df: Counter = Counter()
        for c in all_clusters:
            toks = set(_tokens(c.member_texts))
            for w in toks:
                df[w] += 1
        n_classes = len(all_clusters) or 1

        scores: Dict[str, float] = {}
        for token, tf in cluster_counts.items():
            d = df.get(token, 1)
            idf = math.log(1 + n_classes / d)
            scores[token] = (tf / cluster_total) * idf
        return [t for t, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


# ── Canonicalization helper for stored topic names ───────────────────────


class CanonicalTopicIndex:
    """Maps noisy topic names (``"pricing"``, ``"price"``, ``"too expensive"``)
    to a single canonical label.  Backed by a clustering over the
    distinct names observed in the tenant's interactions.

    Intended use pattern:

    1. Periodically (e.g., nightly) collect distinct topic names from the
       last 90 days.
    2. Build the index via :func:`build`.
    3. At query time call :meth:`canonicalize(name)` to get the cluster
       label; unseen names embed lazily and match the nearest centroid
       above threshold.
    """

    def __init__(self, result: ClusteringResult, embedder: Optional[EmbeddingService] = None) -> None:
        self._result = result
        self._embedder = embedder or get_embedding_service()

    @classmethod
    async def build(
        cls,
        names: Iterable[str],
        *,
        threshold: float = 0.55,
        min_cluster_size: int = 1,
    ) -> "CanonicalTopicIndex":
        names_list = list(dict.fromkeys(n.strip().lower() for n in names if n))
        clusterer = TopicClusterer(threshold=threshold, min_cluster_size=min_cluster_size)
        result = await clusterer.cluster(names_list)
        return cls(result)

    @property
    def clusters(self) -> List[TopicCluster]:
        return self._result.clusters

    async def canonicalize(self, name: str) -> str:
        """Return the canonical label for ``name``; falls back to the
        lower-cased input if no cluster meets the similarity threshold.
        """
        if not name:
            return name
        vec = (await self._embedder.embed([name]))[0]
        best = -1
        best_sim = -1.0
        for c in self._result.clusters:
            sim = cosine_similarity(c.centroid, vec)
            if sim > best_sim:
                best_sim = sim
                best = c.cluster_id
        if best >= 0 and best_sim >= self._result.threshold:
            return self._result.clusters[best].label
        return name.lower()


# ── Convenience: aggregate counts using canonical labels ─────────────────


def aggregate_counts_canonical(
    counts: Dict[str, int],
    index: CanonicalTopicIndex,
) -> Dict[str, int]:
    """Fold a raw ``{name: count}`` dict into ``{canonical_name: count}``.

    Synchronous helper that assumes ``counts`` keys are already in the
    index (i.e., were part of :func:`build`).  Unknown keys are mapped
    to themselves.
    """
    out: Dict[str, int] = {}
    label_by_member: Dict[str, str] = {}
    for cluster in index.clusters:
        for member in cluster.member_texts:
            label_by_member[member] = cluster.label
    for name, c in counts.items():
        canonical = label_by_member.get(name.lower(), name.lower())
        out[canonical] = out.get(canonical, 0) + c
    return out
