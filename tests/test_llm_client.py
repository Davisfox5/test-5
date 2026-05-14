"""Tests for the tiered ``max_tokens`` policy in ``llm_client``."""

from backend.app.services.llm_client import compute_max_tokens


def test_haiku_baseline_returns_base_for_short_input():
    assert compute_max_tokens("haiku") == 1024


def test_sonnet_baseline_returns_base_for_short_input():
    assert compute_max_tokens("sonnet") == 2048


def test_opus_baseline_returns_base_for_short_input():
    assert compute_max_tokens("opus") == 4096


def test_input_length_expands_budget_up_to_2x_base():
    # 16k input tokens hits the cap of the expansion factor.
    assert compute_max_tokens("sonnet", input_tokens=16_000) == 4096


def test_expansion_clamped_to_tier_ceiling():
    # Even with a giant input, haiku tier never exceeds its ceiling (2048).
    assert compute_max_tokens("haiku", input_tokens=1_000_000) == 2048


def test_main_analysis_high_complexity_gets_full_ceiling():
    # Sonnet ceiling is 65536 (raised to fit long-form structured
    # analysis with all 14 fields including coaching / evidence /
    # rubric / methodology).
    out = compute_max_tokens(
        "sonnet",
        input_tokens=2000,
        task_type="main_analysis",
        complexity_score=0.9,
    )
    assert out == 65536


def test_main_analysis_always_gets_ceiling_regardless_of_complexity():
    # The earlier behavior gated the ceiling on complexity > 0.8 OR
    # input > 4000 tokens, which left every short low-complexity call
    # capped at ~2-4K output tokens — well below what the structured-
    # analysis JSON spec actually needs. Diagnostic stamps in
    # production caught the bug: a 24-segment chat had budget=2421
    # yet stop_reason='max_tokens' at 9258 chars of output. Policy is
    # now: main_analysis always gets the ceiling. ``max_tokens`` is
    # an upper bound, not a target, so this only costs more on calls
    # that actually generate more.
    out = compute_max_tokens(
        "sonnet",
        input_tokens=2000,
        task_type="main_analysis",
        complexity_score=0.3,
    )
    assert out == 65536


def test_explicit_override_is_honored_within_ceiling():
    assert compute_max_tokens("sonnet", explicit_override=3000) == 3000


def test_explicit_override_clamped_to_ceiling():
    # Caller asks for 10k on haiku; gets clamped to haiku's 2048 ceiling.
    assert compute_max_tokens("haiku", explicit_override=10_000) == 2048


def test_unknown_tier_falls_back_to_sonnet_defaults():
    assert compute_max_tokens("nonsense-tier") == 2048


def test_floor_prevents_zero_budget():
    # Edge case: explicit_override=1 still returns at least the floor.
    assert compute_max_tokens("haiku", explicit_override=1) >= 256


def test_negative_input_tokens_treated_as_zero():
    assert compute_max_tokens("sonnet", input_tokens=-500) == 2048
