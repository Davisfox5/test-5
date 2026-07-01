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
    # Defaults must match what shipped before centralization (behavior-preserving).
    assert model_catalog.model_for_tier("haiku") == "claude-haiku-4-5-20251001"
    assert model_catalog.model_for_tier("sonnet") == "claude-sonnet-4-6"
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
    monkeypatch.setenv("ANTHROPIC_MODEL_SONNET", "claude-sonnet-5")
    try:
        assert model_catalog.model_for_tier("sonnet") == "claude-sonnet-5"
    finally:
        get_settings.cache_clear()


def test_no_sampling_param_set_is_centralized():
    # Opus 4.7+/Fable reject temperature; the guard set lives in the catalog.
    assert "claude-opus-4-8" in model_catalog.NO_SAMPLING_PARAM_MODELS
    assert "claude-fable-5" in model_catalog.NO_SAMPLING_PARAM_MODELS
    assert model_catalog.rejects_sampling_params("claude-opus-4-8") is True
    assert model_catalog.rejects_sampling_params("claude-haiku-4-5-20251001") is False


def test_failover_tier_map_degrades_downward():
    # A model-unavailable failover should step DOWN a tier, never up.
    assert model_catalog.failover_tier("opus") == "sonnet"
    assert model_catalog.failover_tier("sonnet") == "haiku"
    # Cheapest tier has nowhere lower to go.
    assert model_catalog.failover_tier("haiku") is None


# ── Guard: no stray hardcoded model ids outside the catalog ───────────────


def test_no_hardcoded_model_ids_in_services_or_api():
    """The catalog is the only place a ``claude-*`` literal may appear in the
    runtime service/api tree. Anything else forks the source of truth and
    reintroduces the 25-file blast radius."""
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    roots = [
        repo_root / "backend" / "app" / "services",
        repo_root / "backend" / "app" / "api",
    ]
    allowed = {repo_root / "backend" / "app" / "services" / "model_catalog.py"}
    literal = re.compile(r"[\"']claude-[a-z0-9]")

    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if path in allowed:
                continue
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if literal.search(line):
                    offenders.append(f"{path.relative_to(repo_root)}:{i}: {line.strip()}")

    assert not offenders, "Hardcoded model ids must move to model_catalog:\n" + "\n".join(offenders)
