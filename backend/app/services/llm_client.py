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
# History:
# * 4096 was insufficient — earnings calls hit it.
# * 8192 was insufficient too — coaching / evidence / rubric / methodology
#   / churn / upsell are the LAST fields in the JSON shape and 6/10 voice
#   + 35/51 chat calls landed with json-repair recovery and most of
#   those trailing fields blank.
# * 16384 helped but ~2/3 long sales calls (>40K input chars) still
#   clipped — observed in the post-segmentation verification.
# * 32768 gives ~4× headroom over the typical pre-truncation size of
#   ~8K output tokens. Sonnet 4.6 supports up to 64K natively, so this
#   is still half the available cap.
_CEILING_MAX_TOKENS = {"haiku": 2048, "sonnet": 32768, "opus": 32768}


def compute_max_tokens(
    tier: str,
    *,
    input_tokens: int = 0,
    task_type: Optional[str] = None,
    complexity_score: Optional[float] = None,
    explicit_override: Optional[int] = None,
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

    Returns:
        An integer ``max_tokens`` value safe to pass to ``messages.create``.
    """
    tier_key = (tier or "sonnet").lower()
    base = _BASE_MAX_TOKENS.get(tier_key, _BASE_MAX_TOKENS["sonnet"])
    ceiling = _CEILING_MAX_TOKENS.get(tier_key, _CEILING_MAX_TOKENS["sonnet"])

    expansion = 1.0 + min(max(input_tokens, 0) / 8000.0, 2.0) * 0.5  # 1.0×..2.0×
    budget = min(int(base * expansion), ceiling)

    if explicit_override is not None and explicit_override > 0:
        budget = min(explicit_override, ceiling)

    # Main-analysis ceiling boost — fire on EITHER high complexity OR
    # long input. The original gate keyed only on ``complexity_score >
    # 0.8``, which left long-but-low-complexity calls (e.g. 15-min
    # earnings call narration) truncating mid-JSON when the structured
    # output exceeds the base+expansion budget. Use either signal so
    # the response actually fits.
    is_long_input = input_tokens > 4000
    is_complex = complexity_score is not None and complexity_score > 0.8
    if task_type == "main_analysis" and (is_complex or is_long_input):
        budget = ceiling

    return max(budget, 256)  # never go below a usable floor
