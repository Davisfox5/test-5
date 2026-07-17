"""Shared Anthropic clients + token-budget helper.

A single factory keeps connection pools and TLS sessions hot across services
instead of re-initializing on every request. Services that previously did
``anthropic.AsyncAnthropic(api_key=...)`` per instance should depend on
``get_async_anthropic()`` (or its sync sibling) instead.

``compute_max_tokens`` is the project-wide ``max_tokens`` policy: tier-aware
defaults that scale with input length, with a hard ceiling per tier and an
explicit-override path for callers that know they need more.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from functools import lru_cache
from typing import Any, Awaitable, Callable, Optional

import anthropic

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


class _LoopLocalAsyncAnthropic:
    """Proxy that resolves to a per-event-loop ``AsyncAnthropic``.

    httpx binds pooled connections to the event loop that created them.
    Celery sync task bodies call LLM code through ``asyncio.run`` (a fresh
    loop per call), so a single process-wide async client would hand out
    keep-alive connections bound to an already-closed loop — the same
    "RuntimeError: Event loop is closed" class that ``_run_async`` in
    ``tasks.py`` dispose-firsts away for the asyncpg pool.

    Resolution happens per attribute access, i.e. at await time inside
    whatever loop is running — not at service-constructor time — so the
    many services that capture ``self._client = get_async_anthropic()``
    in ``__init__`` (often before any loop exists) still get a client
    owned by the loop that ultimately makes the request. Within one
    long-lived loop (the FastAPI app, one ``asyncio.run`` body) the pool
    stays hot exactly as before; clients for finished loops are dropped
    when their loop is garbage-collected.
    """

    __slots__ = ("_by_loop", "_no_loop_client")

    def __init__(self) -> None:
        self._by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anthropic.AsyncAnthropic]" = (
            weakref.WeakKeyDictionary()
        )
        # Attribute reads outside any loop (e.g. introspection in sync
        # code) get a real client; it never serves requests, because a
        # request's attribute chain resolves inside the running loop.
        self._no_loop_client: Optional[anthropic.AsyncAnthropic] = None

    def _resolve(self) -> anthropic.AsyncAnthropic:
        settings = get_settings()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._no_loop_client is None:
                self._no_loop_client = anthropic.AsyncAnthropic(
                    api_key=settings.ANTHROPIC_API_KEY
                )
            return self._no_loop_client
        client = self._by_loop.get(loop)
        if client is None:
            client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
            self._by_loop[loop] = client
        return client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        return "<loop-local AsyncAnthropic proxy>"


@lru_cache(maxsize=1)
def get_async_anthropic() -> anthropic.AsyncAnthropic:
    """Return the shared AsyncAnthropic client (loop-local under the hood).

    The returned object is a ``_LoopLocalAsyncAnthropic`` proxy that is
    API-compatible with ``anthropic.AsyncAnthropic``; see its docstring
    for why the real client is scoped to the running event loop.
    """
    return _LoopLocalAsyncAnthropic()  # type: ignore[return-value]


@lru_cache(maxsize=1)
def get_anthropic() -> anthropic.Anthropic:
    """Return a process-wide synchronous Anthropic client (for Celery tasks)."""
    settings = get_settings()
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


# ── Transient-retry + model-failover wrapper ──────────────────────────────
#
# A single model call is a failure surface: a provider blip (429/5xx/timeout)
# or a model that's been deprecated/suspended (404) should not turn into a
# failed customer request when a cheaper tier could serve it. This wrapper is
# the one place that resilience lives. Policy:
#   * transient errors  → retry on the SAME model, exponential backoff, capped;
#   * model-unavailable → fail over to a cheaper-tier model id once, no waste;
#   * anything else (400/401/403/422) → re-raise (masking a real bug is worse).
# Every retry/failover logs its reason so failovers are observable.

# Real SDK types we treat as transient. Kept broad but conservative.
_TRANSIENT_TYPES = (
    anthropic.RateLimitError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)
_TRANSIENT_STATUS = {429, 500, 502, 503, 529}
_UNAVAILABLE_STATUS = {404}


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, _TRANSIENT_TYPES):
        return True
    # Name/status fallbacks so the classifier is testable without constructing
    # httpx-backed SDK exceptions, and robust across SDK versions.
    if type(exc).__name__ in {
        "RateLimitError", "APITimeoutError", "APIConnectionError",
        "InternalServerError", "OverloadedError",
    }:
        return True
    return getattr(exc, "status_code", None) in _TRANSIENT_STATUS


def _is_model_unavailable(exc: Exception) -> bool:
    if isinstance(exc, getattr(anthropic, "NotFoundError", ())):
        return True
    if type(exc).__name__ == "NotFoundError":
        return True
    return getattr(exc, "status_code", None) in _UNAVAILABLE_STATUS


# Per-entry Batches API error types (``result.error.type``) worth a second
# attempt on a lower tier: transient overloads / timeouts, and a model that
# came back unavailable (404). Deterministic client errors
# (``invalid_request_error`` / ``authentication_error`` / ``permission_error``)
# are the caller's bug — retrying them on another tier just burns tokens.
# Mirrors the live-path split in ``_is_transient`` / ``_is_model_unavailable``.
_RETRYABLE_BATCH_ERROR_TYPES = frozenset({
    "overloaded_error",
    "api_error",
    "rate_limit_error",
    "timeout_error",
    "not_found_error",
})


def batch_error_is_retryable(error_type: Optional[str]) -> bool:
    """True when a per-entry batch error should be retried on the fallback tier."""
    return error_type in _RETRYABLE_BATCH_ERROR_TYPES


async def acreate_with_failover(
    client: Any,
    *,
    model: str,
    fallback_model: Optional[str] = None,
    max_retries: int = 2,
    base_delay: float = 0.5,
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **create_kwargs: Any,
) -> Any:
    """Call ``client.messages.create(model=..., **create_kwargs)`` with bounded
    transient retries and a single model failover.

    Args:
        model: primary model id.
        fallback_model: cheaper-tier model id to fail over to when the primary
            is unavailable or exhausts its transient retries. ``None`` disables
            failover (the last error re-raises).
        max_retries: transient retries per model (so up to ``max_retries + 1``
            attempts on each model). Non-transient errors never retry.
        base_delay: exponential-backoff base (``base_delay * 2**attempt``).
        _sleep: injectable sleep (tests pass a no-op).

    Raises :class:`llm_circuit_breaker.LLMCallsSuspended` (without
    touching the API) while the shared credit-balance breaker is open,
    and opens that breaker on a credit-balance 400 — an expected
    operational state that must not error once per call.
    """
    from backend.app.services import llm_circuit_breaker as _breaker

    await _breaker.guard(client)

    active = model
    used_fallback = False
    attempt = 0
    while True:
        try:
            return await client.messages.create(model=active, **create_kwargs)
        except Exception as exc:  # noqa: BLE001 — classified below, re-raised if unknown
            if _breaker.record_billing_failure(exc):
                raise _breaker.LLMCallsSuspended(
                    "LLM calls paused: Anthropic credit balance exhausted"
                ) from exc
            if _is_model_unavailable(exc):
                if fallback_model and not used_fallback:
                    logger.warning(
                        "llm model %s unavailable (%s); failover to %s",
                        active, type(exc).__name__, fallback_model,
                    )
                    active, used_fallback, attempt = fallback_model, True, 0
                    continue
                raise
            if _is_transient(exc):
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "transient llm error on %s (attempt %d/%d): %s; retrying in %.2fs",
                        active, attempt + 1, max_retries, type(exc).__name__, delay,
                    )
                    await _sleep(delay)
                    attempt += 1
                    continue
                if fallback_model and not used_fallback:
                    logger.warning(
                        "llm %s exhausted %d transient retries (%s); failover to %s",
                        active, max_retries, type(exc).__name__, fallback_model,
                    )
                    active, used_fallback, attempt = fallback_model, True, 0
                    continue
                raise
            # Non-transient, not unavailable (400/401/403/422) — surface it.
            raise


# ── Tiered max_tokens policy ──────────────────────────────────────────────
#
# Replaces the prior flat ``max_tokens=8192`` in ai_analysis with a budget
# that scales with input size and respects a per-tier ceiling. The ceiling
# matches today's flat cap for the heaviest tier (Opus, 8192) — nothing
# legitimately long is ever truncated; we just stop allocating 8K for short
# calls. Average savings on output token spend: ~40-55%.

# Typical observed completion size by tier.
_BASE_MAX_TOKENS = {"haiku": 1024, "sonnet": 2048, "opus": 4096}
# Hard upper bound — explicit overrides are still capped here.
#
# Set to 8K based on observed natural output sizes. The earlier
# escalation through 8K→16K→32K→64K chased the wrong problem: the
# ``is_complex or is_long_input`` gate was suppressing every short
# call to ~2K, so the ceiling was never being applied. With the gate
# removed (commit 31a541a), 8K is plenty:
#   * Voice calls naturally generate well under 8K (verified)
#   * Chat truncation we saw earlier was the gate firing at 2421,
#     not the 8K cap actually being hit
# If a tenant's calls genuinely need more than 8K of structured
# output later, raise this knob then — measurement first.
_CEILING_MAX_TOKENS = {"haiku": 2048, "sonnet": 8192, "opus": 8192}


def compute_max_tokens(
    tier: str,
    *,
    input_tokens: int = 0,
    task_type: Optional[str] = None,
    complexity_score: Optional[float] = None,
    explicit_override: Optional[int] = None,
    call_site: Optional[str] = None,
) -> int:
    """Return a sane ``max_tokens`` for an Anthropic call.

    Args:
        tier: ``"haiku"``, ``"sonnet"``, or ``"opus"``. Unknown tiers fall
            back to sonnet defaults.
        input_tokens: Rough size of the prompt. Longer inputs typically
            warrant longer completions, so the budget scales linearly with
            input up to 2× the BASE for that tier.
        task_type: Optional caller-defined label. ``"main_analysis"``
            paired with ``complexity_score > 0.8`` gets the full ceiling
            so dense interaction analysis isn't truncated.
        complexity_score: 0.0-1.0, e.g. ``Interaction.complexity_score``.
        explicit_override: Caller-supplied cap. Honored, but still clamped
            to the tier's ceiling so a misbehaving caller can't escalate
            cost.
        call_site: Optional stable identifier used to look up a learned
            ceiling from ``llm_ceiling_recommendation``. When a learned
            ceiling exists, it replaces the static ceiling for this call
            (still subject to the absolute cap below).

    Returns:
        An integer ``max_tokens`` value safe to pass to ``messages.create``.
    """
    tier_key = (tier or "sonnet").lower()
    base = _BASE_MAX_TOKENS.get(tier_key, _BASE_MAX_TOKENS["sonnet"])
    static_ceiling = _CEILING_MAX_TOKENS.get(tier_key, _CEILING_MAX_TOKENS["sonnet"])
    ceiling = static_ceiling

    if call_site is not None:
        try:
            from backend.app.services.llm_telemetry import learned_ceiling

            learned = learned_ceiling(call_site, tier_key)
            if learned is not None:
                # Never exceed the static tier cap — a learned ceiling
                # high enough to bust the static cap means the call site
                # is doing something unintended; raise the static cap
                # deliberately instead of silently.
                ceiling = min(learned, static_ceiling)
        except Exception:  # pragma: no cover — telemetry is optional
            pass

    expansion = 1.0 + min(max(input_tokens, 0) / 8000.0, 2.0) * 0.5  # 1.0×..2.0×
    budget = min(int(base * expansion), ceiling)

    if explicit_override is not None and explicit_override > 0:
        budget = min(explicit_override, ceiling)

    # Main-analysis ALWAYS gets the ceiling. The structured-analysis
    # output is roughly constant-size regardless of input length
    # (we ask for the same set of fields whether the call is 2 min
    # or 30 min), so gating the ceiling on input length was a
    # premature optimization — it left every call shorter than 4K
    # input tokens truncating at base*expansion (~2-4K output tokens).
    # The diagnostic stamps caught it: a 24-segment chat call had
    # budget=2421 yet stop_reason='max_tokens' at 9258 chars of
    # output — the call wanted to emit more but was artificially
    # capped well below the configured 64K ceiling.
    #
    # Note: ``max_tokens`` is an upper bound, not a target. We don't
    # pay for the budget — only for what the model actually generates.
    # So budgeting high is free; the cost variation lives in the
    # natural verbosity of the output, not the cap.
    if task_type == "main_analysis":
        budget = ceiling

    return max(budget, 256)  # never go below a usable floor
