"""Prometheus metrics for the continuous-improvement system.

Single module — every producer + worker imports the metrics it needs from
here.  ``prometheus_client`` is an optional dependency: if it isn't
installed, all metric calls become no-ops so the application keeps running.

Mount the ``/metrics`` endpoint via :func:`metrics_handler` from
``main.py`` to expose them.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _NoopMetric:
    """Stand-in used when prometheus_client isn't installed."""

    def labels(self, *_args: Any, **_kwargs: Any) -> "_NoopMetric":
        return self

    def inc(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: D401
        return None

    def observe(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set(self, *_args: Any, **_kwargs: Any) -> None:
        return None


try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _ENABLED = True
except ImportError:  # pragma: no cover
    _ENABLED = False
    Counter = Gauge = Histogram = lambda *a, **kw: _NoopMetric()  # type: ignore
    CONTENT_TYPE_LATEST = "text/plain"

    def generate_latest() -> bytes:  # type: ignore
        return b""


# ── Quality / feedback metrics ───────────────────────────────────────────

QUALITY_SCORE = Histogram(
    "callsight_insight_quality_score",
    "LLM-judge composite quality scores per producer.",
    ["tenant", "surface", "model", "channel"],
    buckets=(0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0),
)

FEEDBACK_EVENTS = Counter(
    "callsight_feedback_events_total",
    "Feedback events received per surface and event_type.",
    ["tenant", "surface", "event_type"],
)

REPLY_EDIT_DISTANCE = Histogram(
    "callsight_reply_edit_distance",
    "Normalised edit distance between drafted and sent reply bodies.",
    ["tenant", "variant_id"],
    buckets=(0.0, 0.05, 0.10, 0.20, 0.40, 0.70, 1.0),
)

CLASSIFICATION_OVERRIDE = Counter(
    "callsight_classification_override_total",
    "Email-classifier overrides registered by users.",
    ["tenant"],
)


# ── Producer + judge runtime metrics ─────────────────────────────────────

LLM_LATENCY = Histogram(
    "callsight_llm_latency_seconds",
    "Anthropic API call latency per surface + model.",
    ["surface", "model"],
)

LLM_JUDGE_COST = Counter(
    "callsight_llm_judge_cost_dollars_total",
    "Approximate cumulative cost (USD) of LLM judge calls.",
    ["surface"],
)

PROMPT_VARIANT_USAGE = Counter(
    "callsight_prompt_variant_usage_total",
    "Producer calls grouped by chosen prompt variant.",
    ["surface", "variant_id", "status"],
)

ACTIVE_AB_TESTS = Gauge(
    "callsight_active_ab_tests",
    "Number of running A/B prompt experiments.",
    ["surface"],
)

WER_GAUGE = Gauge(
    "callsight_asr_wer_7d",
    "Trailing-7-day word error rate per (tenant, engine, channel).",
    ["tenant", "engine", "channel"],
)

RAG_RETRIEVAL_LATENCY = Histogram(
    "callsight_rag_retrieval_latency_seconds",
    "RAG retrieval latency from kb_document_retrieval.",
    ["tenant", "surface"],
)


# ── Endpoint helpers ─────────────────────────────────────────────────────


def metrics_handler() -> tuple[bytes, str]:
    """Return ``(payload, content_type)`` for a /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST


def is_enabled() -> bool:
    return _ENABLED
