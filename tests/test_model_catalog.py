"""Tests for the single-source-of-truth model catalog.

The catalog is the ONE place a Claude model id / version resolves. These
tests pin the contract:

* every tier resolves to the configured id (env-overridable, defaults pinned);
* the no-sampling-param set and tier-failover map are centralized here;
* a GUARD test proves no service/api module reintroduces a stray hardcoded
  ``claude-*`` literal (which would fork the source of truth again).
"""

from __future__ import annotations

import pathlib
import re

import pytest

from backend.app.services import model_catalog


# ── Catalog API ───────────────────────────────────────────────────────────


def test_tiers_resolve_to_pinned_defaults():
    # Pinned current-generation defaults (Sonnet bumped 4-6 -> 5).
    assert model_catalog.model_for_tier("haiku") == "claude-haiku-4-5-20251001"
    assert model_catalog.model_for_tier("sonnet") == "claude-sonnet-5"
    assert model_catalog.model_for_tier("opus") == "claude-opus-4-8"


def test_convenience_constants_match_resolver():
    assert model_catalog.HAIKU == model_catalog.model_for_tier("haiku")
    assert model_catalog.SONNET == model_catalog.model_for_tier("sonnet")
    assert model_catalog.OPUS == model_catalog.model_for_tier("opus")


def test_unknown_tier_falls_back_to_sonnet():
    assert model_catalog.model_for_tier("nope") == model_catalog.model_for_tier("sonnet")


def test_env_override_changes_resolution(monkeypatch):
    # Model choice must be a runtime decision: an env override wins, but a
    # missing override keeps the pinned default (no silent "latest").
    from backend.app.config import get_settings

    get_settings.cache_clear()
    # Override to a value distinct from the pinned default to prove the env wins.
    monkeypatch.setenv("ANTHROPIC_MODEL_SONNET", "claude-sonnet-4-6")
    try:
        assert model_catalog.model_for_tier("sonnet") == "claude-sonnet-4-6"
    finally:
        get_settings.cache_clear()


def test_no_sampling_param_set_is_centralized():
    # Opus 4.7+/Fable/Sonnet 5 reject temperature; the guard set lives here.
    assert "claude-opus-4-8" in model_catalog.NO_SAMPLING_PARAM_MODELS
    assert "claude-fable-5" in model_catalog.NO_SAMPLING_PARAM_MODELS
    assert model_catalog.rejects_sampling_params("claude-opus-4-8") is True
    assert model_catalog.rejects_sampling_params("claude-haiku-4-5-20251001") is False


def test_sonnet5_rejects_sampling_params():
    # Sonnet 5 (the current Sonnet default) 400s on temperature/top_p/top_k —
    # it MUST be in the no-sampling set or every Sonnet call errors.
    assert model_catalog.SONNET == "claude-sonnet-5"
    assert model_catalog.rejects_sampling_params("claude-sonnet-5") is True


def test_thinking_on_by_default_flags_sonnet5():
    # Sonnet 5 runs adaptive thinking when `thinking` is omitted; the older
    # ids do not. The catalog flags which models need an explicit suppression.
    assert model_catalog.thinking_on_by_default("claude-sonnet-5") is True
    assert model_catalog.thinking_on_by_default("claude-sonnet-4-6") is False
    assert model_catalog.thinking_on_by_default("claude-haiku-4-5-20251001") is False
    assert model_catalog.thinking_on_by_default("claude-opus-4-8") is False


def test_tier_for_model_reverse_maps():
    assert model_catalog.tier_for_model(model_catalog.HAIKU) == "haiku"
    assert model_catalog.tier_for_model(model_catalog.SONNET) == "sonnet"
    assert model_catalog.tier_for_model(model_catalog.OPUS) == "opus"
    assert model_catalog.tier_for_model("some-unknown-model") is None


def test_failover_tier_map_degrades_downward():
    # A model-unavailable failover should step DOWN a tier, never up.
    assert model_catalog.failover_tier("opus") == "sonnet"
    assert model_catalog.failover_tier("sonnet") == "haiku"
    # Cheapest tier has nowhere lower to go.
    assert model_catalog.failover_tier("haiku") is None


# ── Guard: no stray hardcoded model ids outside the catalog ───────────────


def test_no_hardcoded_model_ids_in_runtime_tree():
    """The catalog (and the config defaults it reads) are the only places a
    ``claude-*`` literal may appear anywhere in ``backend/`` or ``scripts/``.
    Anything else forks the source of truth and reintroduces the 25-file blast
    radius. Alembic versions are immutable history and stay allowlisted."""
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    roots = [repo_root / "backend", repo_root / "scripts"]
    allowed_files = {
        repo_root / "backend" / "app" / "services" / "model_catalog.py",
        # The env-overridable pinned defaults the catalog resolves from.
        repo_root / "backend" / "app" / "config.py",
    }
    # Migration files are frozen once merged; editing them to chase a rename
    # would rewrite history. New migrations should not add model literals.
    allowed_dirs = [repo_root / "backend" / "alembic" / "versions"]
    literal = re.compile(r"[\"']claude-[a-z0-9]")

    offenders: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path in allowed_files:
                continue
            if any(d in path.parents for d in allowed_dirs):
                continue
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if literal.search(line):
                    offenders.append(f"{path.relative_to(repo_root)}:{i}: {line.strip()}")

    assert not offenders, "Hardcoded model ids must move to model_catalog:\n" + "\n".join(offenders)
