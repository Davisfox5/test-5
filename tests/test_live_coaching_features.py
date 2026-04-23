"""Tests for the deterministic live-coaching primitives."""

import pytest

from backend.app.services.live_coaching_features import (
    LFTriggerScanner,
    LiveFeatureWindow,
    LiveTurn,
)


def _turn(speaker_id, text, start, end, is_agent):
    return LiveTurn(
        speaker_id=speaker_id, text=text, start=start, end=end, is_agent=is_agent
    )


def test_window_empty_snapshot_returns_zeros():
    snap = LiveFeatureWindow().snapshot()
    assert snap.window_sec == 0.0
    assert snap.rep_talk_pct == 0.0
    assert snap.patience_sec is None


def test_window_trims_turns_beyond_the_rolling_window():
    w = LiveFeatureWindow(window_sec=10.0)
    # Turn at t=0, ends at t=5 — should be trimmed when we push a turn
    # ending at t=20 (cutoff becomes 10; prior turn ended at 5).
    w.push(_turn("c", "hello", 0.0, 5.0, False))
    w.push(_turn("a", "hi back", 19.0, 20.0, True))
    snap = w.snapshot()
    # Window_sec should reflect only the 1s retained turn.
    assert snap.window_sec <= 2.0


def test_window_computes_talk_listen_and_interactivity():
    w = LiveFeatureWindow(window_sec=60.0)
    w.push(_turn("a", "hello there", 0.0, 2.0, True))
    w.push(_turn("c", "hi", 2.0, 4.0, False))
    w.push(_turn("a", "how are you today", 4.0, 6.0, True))
    snap = w.snapshot()
    assert snap.rep_talk_pct > 0
    assert snap.customer_talk_pct > 0
    # Two speaker switches in ~6 seconds → high per-minute rate.
    assert snap.interactivity_per_min > 10


def test_window_reports_patience_as_gap_before_agent():
    w = LiveFeatureWindow(window_sec=60.0)
    w.push(_turn("c", "I have a question", 0.0, 3.0, False))
    w.push(_turn("a", "sure", 4.0, 5.0, True))
    assert w.snapshot().patience_sec == pytest.approx(1.0)


def test_window_flags_back_channel_gap_when_agent_is_silent():
    w = LiveFeatureWindow(window_sec=120.0)
    w.push(_turn("a", "tell me more", 0.0, 1.0, True))
    w.push(_turn("c", "long customer monologue here that keeps going", 1.0, 30.0, False))
    snap = w.snapshot()
    assert snap.back_channel_gap_sec is not None
    assert snap.back_channel_gap_sec >= 25  # at least the customer-turn length


def test_window_back_channel_gap_is_zero_right_after_agent_acknowledgement():
    w = LiveFeatureWindow(window_sec=60.0)
    w.push(_turn("c", "customer talking", 0.0, 10.0, False))
    w.push(_turn("a", "mm-hmm", 10.0, 10.3, True))
    assert w.snapshot().back_channel_gap_sec == 0.0


# ── LF trigger scanner ──────────────────────────────────────────────────


def test_scanner_fires_cancel_intent_on_customer_turn():
    scanner = LFTriggerScanner(cooldown_sec=0)
    w = LiveFeatureWindow()
    turn = _turn("c", "I think we're cancelling next month", 0.0, 3.0, False)
    w.push(turn)
    alerts = scanner.push(turn, w)
    assert any(a.kind == "cancel_intent" for a in alerts)


def test_scanner_fires_commitment_on_customer_turn():
    scanner = LFTriggerScanner(cooldown_sec=0)
    w = LiveFeatureWindow()
    turn = _turn("c", "Let's do it, send over the contract", 0.0, 3.0, False)
    w.push(turn)
    alerts = scanner.push(turn, w)
    assert any(a.kind == "commitment" for a in alerts)


def test_scanner_respects_cooldown_between_alerts():
    scanner = LFTriggerScanner(cooldown_sec=60.0)
    w = LiveFeatureWindow()
    turn = _turn("c", "we're cancelling", 0.0, 3.0, False)
    w.push(turn)
    first = scanner.push(turn, w)
    second = scanner.push(turn, w)
    assert any(a.kind == "cancel_intent" for a in first)
    assert not any(a.kind == "cancel_intent" for a in second)


def test_scanner_flags_agent_monologue_after_threshold():
    scanner = LFTriggerScanner(cooldown_sec=0)
    w = LiveFeatureWindow(window_sec=120.0)
    long_turn = _turn("a", "a " * 200, 0.0, 75.0, True)
    w.push(long_turn)
    alerts = scanner.push(long_turn, w)
    assert any(a.kind == "monologue" for a in alerts)


def test_scanner_abstains_on_benign_customer_turn():
    scanner = LFTriggerScanner(cooldown_sec=0)
    w = LiveFeatureWindow()
    turn = _turn("c", "thanks for the update", 0.0, 2.0, False)
    w.push(turn)
    alerts = scanner.push(turn, w)
    # No cancel, no commitment, no monologue → no alerts.
    assert not alerts
