"""Model catalog — the single source of truth for Claude model ids.

Every runtime LLM touchpoint resolves *which model / version* it calls
through this module. Before it existed, ~27 hardcoded ``claude-*`` strings
were scattered across ~25 files plus three separate tier→id maps that had to
be hand-kept-in-sync; a single deprecation/suspension (the June 2026 Fable 5
event being the illustrative risk) meant editing every one of them.

Now:

* Tier ids resolve from :mod:`backend.app.config` (env-overridable, defaults
  pinned to today's shipping ids), so a version bump is a one-line, reviewable
  change — never a "silently pull latest".
* The no-sampling-parameter set (models that 400 on ``temperature``) lives
  here so the temperature guard has one owner.
* A tier-failover map (used by the transient-error/failover wrapper in
  :mod:`backend.app.services.llm_client`) degrades DOWN a tier when a model
  is unavailable — never up.

Callers should prefer :func:`model_for_tier` (live, reads settings) or the
convenience module constants ``HAIKU`` / ``SONNET`` / ``OPUS``.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Optional

from backend.app.config import get_settings


# Canonical tier keys used across the backend.
HAIKU_TIER = "haiku"
SONNET_TIER = "sonnet"
OPUS_TIER = "opus"


def model_for_tier(tier: Optional[str]) -> str:
    """Resolve a tier name (``"haiku"`` / ``"sonnet"`` / ``"opus"``) to its
    configured model id. Unknown tiers fall back to Sonnet — matching the
    historical ``MODELS.get(tier, MODELS["sonnet"])`` behavior of the call
    sites this replaces."""
    settings = get_settings()
    key = (tier or SONNET_TIER).lower()
    if key == HAIKU_TIER:
        return settings.ANTHROPIC_MODEL_HAIKU
    if key == OPUS_TIER:
        return settings.ANTHROPIC_MODEL_OPUS
    # sonnet is also the default for anything unrecognized.
    return settings.ANTHROPIC_MODEL_SONNET


def tier_id_map() -> Dict[str, str]:
    """Return the full ``{tier: model_id}`` map (for callers that kept a dict)."""
    return {
        HAIKU_TIER: model_for_tier(HAIKU_TIER),
        SONNET_TIER: model_for_tier(SONNET_TIER),
        OPUS_TIER: model_for_tier(OPUS_TIER),
    }


def tier_for_model(model_id: Optional[str]) -> Optional[str]:
    """Reverse of :func:`model_for_tier`: given a resolved model id, return its
    tier name, or ``None`` if it isn't one of the configured tier models. Used
    to report the tier a request was *actually* served on after a failover."""
    if not model_id:
        return None
    for tier in (HAIKU_TIER, SONNET_TIER, OPUS_TIER):
        if model_for_tier(tier) == model_id:
            return tier
    return None


# Convenience constants. Resolved at import time from settings; a version
# change (env or config default) takes effect on the next process start,
# which is also when a deploy happens — so this stays a deliberate change.
HAIKU: str = model_for_tier(HAIKU_TIER)
SONNET: str = model_for_tier(SONNET_TIER)
OPUS: str = model_for_tier(OPUS_TIER)


# ── Sampling-parameter capability ─────────────────────────────────────────
#
# Opus 4.7+ and Fable 5 reject ``temperature`` / ``top_p`` / ``top_k`` with a
# 400 ("`temperature` is deprecated for this model"). Sonnet 4.6 / Haiku 4.5
# still accept them. Callers omit sampling params for any id in this set
# rather than downgrade the tier. Add new no-sampling ids here as adopted.
NO_SAMPLING_PARAM_MODELS: FrozenSet[str] = frozenset(
    {
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-fable-5",
        # Sonnet 5 removed the sampling knobs too — a non-default temperature
        # 400s. Without this entry every Sonnet call would error after the
        # 4-6 -> 5 bump.
        "claude-sonnet-5",
    }
)


def rejects_sampling_params(model_id: str) -> bool:
    """True if ``model_id`` 400s on sampling params (temperature/top_p/top_k)."""
    return model_id in NO_SAMPLING_PARAM_MODELS


# ── Default-on adaptive thinking ──────────────────────────────────────────
#
# Sonnet 5 runs *adaptive thinking* whenever the ``thinking`` param is omitted
# (Opus 4.7/4.8 default it OFF; Sonnet 4.6 had no thinking). Our runtime calls
# don't want implicit thinking — it consumes part of ``max_tokens`` (risking
# truncation on tight budgets like Ask Linda's) and adds latency — so the
# router sends an explicit ``thinking={"type": "disabled"}`` for these models
# unless a caller opts in. Add new default-on ids here as we adopt them.
_THINKING_ON_BY_DEFAULT: FrozenSet[str] = frozenset({"claude-sonnet-5"})


def thinking_on_by_default(model_id: str) -> bool:
    """True if ``model_id`` runs adaptive thinking when ``thinking`` is omitted."""
    return model_id in _THINKING_ON_BY_DEFAULT


# ── Tier failover ─────────────────────────────────────────────────────────
#
# When a model id is *unavailable* (deprecated / suspended / 404), fail over
# to the next cheaper tier rather than hard-failing the request. Transient
# errors (429/5xx/timeout) are retried on the SAME model by the wrapper —
# this map is only for genuine model-unavailability.
_FAILOVER_TIER: Dict[str, Optional[str]] = {
    OPUS_TIER: SONNET_TIER,
    SONNET_TIER: HAIKU_TIER,
    HAIKU_TIER: None,
}


def failover_tier(tier: Optional[str]) -> Optional[str]:
    """Return the next cheaper tier to fail over to, or ``None`` at the floor."""
    return _FAILOVER_TIER.get((tier or SONNET_TIER).lower())


def failover_model(tier: Optional[str]) -> Optional[str]:
    """Resolve the failover *model id* for a tier, or ``None`` at the floor."""
    ft = failover_tier(tier)
    return model_for_tier(ft) if ft is not None else None
