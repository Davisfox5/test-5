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
    "linda_insight_quality_score",
    "LLM-judge composite quality scores per producer.",
    ["tenant", "surface", "model", "channel"],
    buckets=(0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0),
)

FEEDBACK_EVENTS = Counter(
    "linda_feedback_events_total",
    "Feedback events received per surface and event_type.",
    ["tenant", "surface", "event_type"],
)

REPLY_EDIT_DISTANCE = Histogram(
    "linda_reply_edit_distance",
    "Normalised edit distance between drafted and sent reply bodies.",
    ["tenant", "variant_id"],
    buckets=(0.0, 0.05, 0.10, 0.20, 0.40, 0.70, 1.0),
)

CLASSIFICATION_OVERRIDE = Counter(
    "linda_classification_override_total",
    "Email-classifier overrides registered by users.",
    ["tenant"],
)


# ── Producer + judge runtime metrics ─────────────────────────────────────

LLM_LATENCY = Histogram(
    "linda_llm_latency_seconds",
    "Anthropic API call latency per surface + model.",
    ["surface", "model"],
)

LLM_JUDGE_COST = Counter(
    "linda_llm_judge_cost_dollars_total",
    "Approximate cumulative cost (USD) of LLM judge calls.",
    ["surface"],
)

PROMPT_VARIANT_USAGE = Counter(
    "linda_prompt_variant_usage_total",
    "Producer calls grouped by chosen prompt variant.",
    ["surface", "variant_id", "status"],
)

ACTIVE_AB_TESTS = Gauge(
    "linda_active_ab_tests",
    "Number of running A/B prompt experiments.",
    ["surface"],
)

WER_GAUGE = Gauge(
    "linda_asr_wer_7d",
    "Trailing-7-day word error rate per (tenant, engine, channel).",
    ["tenant", "engine", "channel"],
)

RAG_RETRIEVAL_LATENCY = Histogram(
    "linda_rag_retrieval_latency_seconds",
    "RAG retrieval latency from kb_document_retrieval.",
    ["tenant", "surface"],
)


# ── Pipeline stage timings ───────────────────────────────────────────────

PIPELINE_STAGE_LATENCY = Histogram(
    "linda_pipeline_stage_seconds",
    "Voice + text pipeline stage durations.",
    # ``stage`` maps to Step 1..17 in tasks._run_pipeline_impl.
    # ``channel`` is voice|email|transcript. ``status`` is success|error.
    ["stage", "channel", "status"],
    # Cover both fast stages (ms-scale — search indexing) and slow
    # ones (minutes — large Whisper transcripts).
    buckets=(0.05, 0.25, 1.0, 3.0, 10.0, 30.0, 120.0, 300.0),
)

PIPELINE_RUNS = Counter(
    "linda_pipeline_runs_total",
    "Pipeline executions, counted per final outcome.",
    ["channel", "status"],
)


# ── Transcription ────────────────────────────────────────────────────────

TRANSCRIPTION_SECONDS = Histogram(
    "linda_transcription_duration_seconds",
    "Time spent inside TranscriptionService.transcribe, per engine + mode.",
    ["engine", "mode"],  # mode=url|file; engine=deepgram|whisper
    buckets=(0.5, 2.0, 5.0, 15.0, 45.0, 120.0, 300.0),
)

TRANSCRIPTION_AUDIO_SECONDS = Counter(
    "linda_transcription_audio_seconds_total",
    "Cumulative audio seconds transcribed — useful for unit-economics dashboards.",
    ["engine"],
)

TRANSCRIPTION_FAILURES = Counter(
    "linda_transcription_failures_total",
    "Transcription attempts that raised. Split by engine + reason class.",
    ["engine", "reason"],  # reason=timeout|auth|server|other
)


# ── Celery queue + worker depth ──────────────────────────────────────────

CELERY_TASK_RUNS = Counter(
    "linda_celery_task_runs_total",
    "Celery task completions.",
    ["task_name", "status"],  # status=success|failure|retry
)

CELERY_TASK_LATENCY = Histogram(
    "linda_celery_task_seconds",
    "Celery task end-to-end duration.",
    ["task_name"],
    buckets=(0.05, 0.25, 1.0, 5.0, 30.0, 120.0, 600.0),
)

CELERY_QUEUE_DEPTH = Gauge(
    "linda_celery_queue_depth",
    "Redis LIST length for each celery queue — sampled periodically.",
    ["queue"],
)


# ── LLM observability ────────────────────────────────────────────────────

LLM_OUTPUT_TOKENS = Histogram(
    "linda_llm_output_tokens",
    "Output tokens per Anthropic completion, sliced by call site + tier. "
    "Feeds the cost dashboards and the adaptive max_tokens learner.",
    ["call_site", "tier"],
    buckets=(128, 256, 512, 1024, 2048, 4096, 8192, 16384),
)

LLM_INPUT_TOKENS = Counter(
    "linda_llm_input_tokens_total",
    "Cumulative billable input tokens per call site + tier "
    "(uncached prompt tokens; cache reads are tracked separately).",
    ["call_site", "tier"],
)

LLM_CACHE_READ_TOKENS = Counter(
    "linda_llm_cache_read_tokens_total",
    "Cumulative input tokens served from Anthropic's prompt cache.",
    ["call_site", "tier"],
)

LLM_CACHE_CREATION_TOKENS = Counter(
    "linda_llm_cache_creation_tokens_total",
    "Cumulative input tokens written into the prompt cache.",
    ["call_site", "tier"],
)

LLM_TRUNCATIONS = Counter(
    "linda_llm_truncations_total",
    "Completions that hit stop_reason='max_tokens'. Watch this alongside "
    "the adaptive ceiling — a sustained > 5% rate is the trigger for "
    "raising the learned ceiling automatically.",
    ["call_site", "tier"],
)


# ── Adaptive ceiling recompute outcome ───────────────────────────────────

LLM_CEILING_RECOMPUTE = Counter(
    "linda_llm_ceiling_recompute_total",
    "Daily ceiling-recompute outcomes (updated|skipped|error).",
    ["status"],
)


# ── CRM ──────────────────────────────────────────────────────────────────

CRM_SYNC_OUTCOMES = Counter(
    "linda_crm_sync_outcomes_total",
    "CRM sync runs, by provider + outcome.",
    ["provider", "status"],  # status=success|partial|failed
)

CRM_WRITEBACK_OUTCOMES = Counter(
    "linda_crm_writeback_outcomes_total",
    "CRM write-back attempts, by provider + kind + outcome.",
    # kind=note|activity|stage ; status=success|capability_missing|error|auth
    ["provider", "kind", "status"],
)


# ── Live telephony ───────────────────────────────────────────────────────

LIVE_SESSIONS = Gauge(
    "linda_live_sessions_active",
    "Concurrent live telephony sessions by provider.",
    ["provider"],  # twilio|signalwire|telnyx
)

LIVE_DEEPGRAM_WS_CONNECTS = Counter(
    "linda_live_deepgram_connects_total",
    "Deepgram live websocket establishments, by outcome.",
    ["status"],  # success|failed
)

LIVE_PARALINGUISTIC_SNAPSHOTS = Counter(
    "linda_live_paralinguistic_snapshots_total",
    "Paralinguistic snapshots produced per live session, by outcome.",
    ["status"],  # emitted|short_buffer|error
)


# ── Endpoint helpers ─────────────────────────────────────────────────────


def metrics_handler() -> tuple[bytes, str]:
    """Return ``(payload, content_type)`` for a /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST


def is_enabled() -> bool:
    return _ENABLED
