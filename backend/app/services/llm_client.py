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

from functools import lru_cache
from typing import Optional

import anthropic

from backend.app.config import get_settings


@lru_cache(maxsize=1)
def get_async_anthropic() -> anthropic.AsyncAnthropic:
    """Return a process-wide AsyncAnthropic client."""
    settings = get_settings()
    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


@lru_cache(maxsize=1)
def get_anthropic() -> anthropic.Anthropic:
    """Return a process-wide synchronous Anthropic client (for Celery tasks)."""
    settings = get_settings()
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


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
