"""Tests for the WebSocket live-coaching message shapes.

Drives the same primitives the ``live_transcription`` handler uses —
``LiveFeatureWindow`` and ``LFTriggerScanner`` — through simulated
Deepgram turn streams, and asserts the shape of the ``alert`` and
``features`` frames the handler sends to the client.

We don't spin up a real WebSocket or Deepgram here; the handler wiring
is a thin adapter and the interesting behaviour lives in the primitives
plus the to_wire() contract the frontend will consume.
"""

import pytest

from backend.app.services.live_coaching_features import (
    CoachingAlert,
    LFTriggerScanner,
    LiveFeatureWindow,
    LiveTurn,
)


# ── Helpers that mirror what websocket.py does per final segment ─────────


def _final_to_turn(speaker, text, ts):
    """Replicate the conversion websocket.py runs inside on_transcript."""
    word_count = len(text.split())
    est_duration = max(0.3, word_count * 0.4)
    return LiveTurn(
        speaker_id=str(speaker) if speaker is not None else "customer",
        text=text,
        start=ts,
        end=ts + est_duration,
        is_agent=(speaker == 0),
    )


def _drive_stream(scanner: LFTriggerScanner, window: LiveFeatureWindow, events):
    """Replay (speaker, text, ts) events through scanner+window; collect
    the alert + features frames the handler would emit."""
    alerts_emitted = []
    features_emitted = []
    finals_since_emit = 0
    last_emit_at = 0.0
    MIN_SEC = 5.0
    EVERY_N = 3

    for speaker, text, ts in events:
        turn = _final_to_turn(speaker, text, ts)
        window.push(turn)
        for alert in scanner.push(turn, window):
            alerts_emitted.append({"type": "alert", **alert.to_wire()})

        finals_since_emit += 1
        if finals_since_emit >= EVERY_N and (ts - last_emit_at) >= MIN_SEC:
            features_emitted.append(
                {"type": "features", **window.snapshot().to_wire()}
            )
            last_emit_at = ts
            finals_since_emit = 0

    return alerts_emitted, features_emitted


# ── Alert shape ──────────────────────────────────────────────────────────


def test_alert_frame_has_type_kind_severity_message_and_timestamp():
    window = LiveFeatureWindow()
    scanner = LFTriggerScanner(cooldown_sec=0)
    alerts, _ = _drive_stream(
        scanner,
        window,
        [(1, "we're cancelling next month", 100.0)],
    )
    assert alerts, "expected at least one alert on a cancel-intent utterance"
    frame = alerts[0]
    assert frame["type"] == "alert"
    for key in ("kind", "severity", "message", "evidence", "t"):
        assert key in frame, f"alert frame missing {key!r}"
    assert frame["kind"] == "cancel_intent"
    assert frame["severity"] == "alert"


def test_commitment_alert_fires_on_commit_language():
    window = LiveFeatureWindow()
    scanner = LFTriggerScanner(cooldown_sec=0)
    alerts, _ = _drive_stream(
        scanner,
        window,
        [(1, "let's do it, send over the contract", 200.0)],
    )
    kinds = [a["kind"] for a in alerts]
    assert "commitment" in kinds


def test_no_alert_on_benign_turn():
    window = LiveFeatureWindow()
    scanner = LFTriggerScanner(cooldown_sec=0)
    alerts, _ = _drive_stream(
        scanner,
        window,
        [(1, "thanks for the update, looking forward to it", 0.0)],
    )
    assert alerts == []


def test_monologue_alert_fires_on_long_agent_turn():
    window = LiveFeatureWindow(window_sec=120.0)
    scanner = LFTriggerScanner(cooldown_sec=0)
    # Very-long agent text ⇒ est_duration > 60s, scanner flags monologue.
    long_text = " ".join(["word"] * 250)
    alerts, _ = _drive_stream(scanner, window, [(0, long_text, 0.0)])
    assert any(a["kind"] == "monologue" for a in alerts)


# ── Cooldown ─────────────────────────────────────────────────────────────


def test_cooldown_suppresses_duplicate_alerts_within_window():
    window = LiveFeatureWindow()
    scanner = LFTriggerScanner(cooldown_sec=60.0)
    alerts, _ = _drive_stream(
        scanner,
        window,
        [
            (1, "we're cancelling", 0.0),
            (1, "we're cancelling", 1.0),  # within cooldown
            (1, "we're cancelling", 2.0),
        ],
    )
    cancel_count = sum(1 for a in alerts if a["kind"] == "cancel_intent")
    assert cancel_count == 1


# ── Features frame shape + throttle ──────────────────────────────────────


def test_features_frame_emits_only_after_threshold_and_min_interval():
    window = LiveFeatureWindow(window_sec=120.0)
    scanner = LFTriggerScanner(cooldown_sec=0)
    # Three turns but all inside 1 second — min-interval should block emit.
    _, features = _drive_stream(
        scanner,
        window,
        [
            (0, "hi", 0.0),
            (1, "hello", 0.2),
            (0, "how are you", 0.5),
        ],
    )
    assert features == []


def test_features_frame_carries_expected_keys():
    window = LiveFeatureWindow(window_sec=120.0)
    scanner = LFTriggerScanner(cooldown_sec=0)
    # Spread turns so both throttle thresholds clear.
    _, features = _drive_stream(
        scanner,
        window,
        [
            (0, "hi there", 0.0),
            (1, "hi back", 2.5),
            (0, "how can i help you today", 7.0),
        ],
    )
    assert features, "expected a features frame once the throttle clears"
    frame = features[0]
    assert frame["type"] == "features"
    for key in (
        "window_sec",
        "rep_talk_pct",
        "customer_talk_pct",
        "silence_pct",
        "patience_sec",
        "interactivity_per_min",
        "filler_rate_per_min",
        "question_rate_per_min",
        "lsm_partial",
        "back_channel_gap_sec",
    ):
        assert key in frame, f"features frame missing {key!r}"


# ── to_wire() round-trips as JSON ────────────────────────────────────────


def test_alert_to_wire_is_json_serialisable():
    import json

    alert = CoachingAlert(
        kind="cancel_intent",
        severity="alert",
        message="test",
        evidence={"confidence": 0.7},
    )
    wire = {"type": "alert", **alert.to_wire()}
    # Must serialise without default= (no datetimes, UUIDs, etc.).
    json.dumps(wire)
