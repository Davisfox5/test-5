"""Pure-logic tests for the cold-outreach helpers (no DB, no LLM)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.services.outreach.common import (
    OutreachConfig,
    SendWindow,
    advance_status,
    compose_footer,
    detect_opt_out,
    extract_message_ids,
    in_send_window,
    local_day_bounds_utc,
    looks_like_bounce,
    normalize_domain,
    parse_config,
    render_placeholders,
)


# ── normalize_domain ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.Foo-Gym.com/pricing?x=1", "foo-gym.com"),
        ("http://foo.com", "foo.com"),
        ("foo.com", "foo.com"),
        ("www.foo.com/", "foo.com"),
        ("foo.com:8080/path", "foo.com"),
        ("mailto:owner@foo.com", "foo.com"),
        ("FOO.COM", "foo.com"),
        ("", None),
        (None, None),
        ("not a domain", None),
        ("localhost", None),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


# ── opt-out detection ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "unsubscribe",
        "Please remove me from your list",
        "STOP",
        "stop.",
        "Take me off this list",
        "please opt me out\nthanks",
        "Do not contact me again",
        "don't email me",
    ],
)
def test_detect_opt_out_positive(text):
    assert detect_opt_out(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Sounds interesting, tell me more",
        "We already stopped using MindBody last year",  # 'stopped using X' ≠ opt-out
        "not interested right now, maybe next quarter",
        None,
        "",
    ],
)
def test_detect_opt_out_negative(text):
    assert detect_opt_out(text) is False


def test_detect_opt_out_ignores_quoted_footer():
    # Our own compliance footer appears in the quoted history of every
    # reply — it must not self-trigger an opt-out.
    reply = "Sure, let's talk Tuesday!\n" + "\n" * 20 + (
        "> If you'd rather not hear from me, just reply \"unsubscribe\"\n"
    )
    assert detect_opt_out(reply) is False


# ── status transitions ──────────────────────────────────────────────────


def test_advance_status_monotonic():
    assert advance_status("new", "contacted") == "contacted"
    assert advance_status("contacted", "replied") == "replied"
    # never backwards
    assert advance_status("demo", "contacted") is None
    assert advance_status("replied", "replied") is None


def test_advance_status_terminal_and_dnc():
    assert advance_status("won", "contacted") is None
    assert advance_status("lost", "replied") is None
    # do_not_contact always wins, even from terminal
    assert advance_status("won", "do_not_contact") == "do_not_contact"
    assert advance_status(None, "do_not_contact") == "do_not_contact"


def test_advance_status_from_null():
    assert advance_status(None, "contacted") == "contacted"


# ── config validation ───────────────────────────────────────────────────


def _valid_config() -> dict:
    return {
        "template": {
            "subject": "Quick question about {business_name}",
            "body": "Hi — noticed you use {current_software}.",
            "sender_name": "Davis Fox",
            "sender_business": "Flex",
            "physical_address": "123 Main St, Nashville, TN 37201",
        },
    }


def test_parse_config_defaults():
    cfg = parse_config(_valid_config())
    assert cfg.mode == "review"
    assert cfg.max_touches == 3
    assert cfg.daily_limit is None
    assert len(cfg.steps) == 2
    assert cfg.send_window.start_hour == 9


def test_parse_config_requires_can_spam_identity():
    raw = _valid_config()
    del raw["template"]["physical_address"]
    with pytest.raises(ValidationError):
        parse_config(raw)


def test_parse_config_rejects_bad_mode_and_provider():
    raw = _valid_config()
    raw["mode"] = "yolo"
    with pytest.raises(ValidationError):
        parse_config(raw)
    raw = _valid_config()
    raw["provider"] = "sendgrid"
    with pytest.raises(ValidationError):
        parse_config(raw)


def test_parse_config_rejects_unknown_timezone():
    raw = _valid_config()
    raw["send_window"] = {"timezone": "Mars/Olympus_Mons"}
    with pytest.raises((ValidationError, Exception)):
        parse_config(raw)


# ── send window / day bounds ────────────────────────────────────────────


def test_in_send_window_weekday_hours():
    window = SendWindow(start_hour=9, end_hour=17, timezone="UTC", days=[1, 2, 3, 4, 5])
    # Wednesday 2026-07-08 10:00 UTC — inside
    assert in_send_window(window, datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc))
    # Wednesday 08:59 — before open
    assert not in_send_window(window, datetime(2026, 7, 8, 8, 59, tzinfo=timezone.utc))
    # Wednesday 17:00 — after close (end_hour exclusive)
    assert not in_send_window(window, datetime(2026, 7, 8, 17, 0, tzinfo=timezone.utc))
    # Saturday 10:00 — weekend
    assert not in_send_window(window, datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc))


def test_in_send_window_respects_timezone():
    window = SendWindow(
        start_hour=9, end_hour=17, timezone="America/New_York", days=[1, 2, 3, 4, 5]
    )
    # 14:00 UTC on a Wednesday == 10:00 New York (EDT) — inside.
    assert in_send_window(window, datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc))
    # 12:00 UTC == 08:00 New York — before open.
    assert not in_send_window(window, datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc))


def test_local_day_bounds_utc():
    window = SendWindow(timezone="America/New_York")
    start, end = local_day_bounds_utc(
        window, datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)
    )
    # NY midnight EDT == 04:00 UTC
    assert start == datetime(2026, 7, 8, 4, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 9, 4, 0, tzinfo=timezone.utc)


# ── footer / placeholders / bounce ──────────────────────────────────────


def test_compose_footer_contains_can_spam_fields():
    cfg = parse_config(_valid_config())
    footer = compose_footer(cfg.template)
    assert "Davis Fox" in footer
    assert "Flex" in footer
    assert "123 Main St" in footer
    assert "unsubscribe" in footer.lower()


def test_render_placeholders():
    out = render_placeholders(
        "Hi {business_name}, you use {current_software}?",
        {"business_name": "Iron Works", "current_software": None},
    )
    assert out == "Hi Iron Works, you use ?"
    # unknown placeholders stay untouched
    assert render_placeholders("{unknown}", {}) == "{unknown}"


def test_looks_like_bounce():
    assert looks_like_bounce("MAILER-DAEMON@googlemail.com", None)
    assert looks_like_bounce("postmaster@example.com", "anything")
    assert looks_like_bounce("someone@example.com", "Undeliverable: hello")
    assert not looks_like_bounce("owner@gym.com", "Re: quick question")


def test_extract_message_ids():
    body = (
        "The following message to <owner@gym.com> was undeliverable.\n"
        "Message-ID: <abc.123@mail.gmail.com>\n"
    )
    # Plain addresses in angle brackets are indistinguishable from
    # Message-IDs — they're harmless candidates (filtered by the DB
    # lookup); what matters is the real Message-ID is captured.
    assert "<abc.123@mail.gmail.com>" in extract_message_ids(body)
    assert extract_message_ids(None) == []
