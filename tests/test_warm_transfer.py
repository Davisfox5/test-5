"""Tests for warm-transfer TwiML and conference-join endpoint.

Warm transfer parks the caller in a conference, then outbound-dials the
transfer target whose answer-TwiML joins the same conference. These
tests cover the pure TwiML helper plus the ``/conference-join`` route
that Twilio fetches after the target answers.
"""

from __future__ import annotations

import pytest

from backend.app.services.telephony.twilio import build_conference_twiml


# ── build_conference_twiml ────────────────────────────────────────────


def test_conference_twiml_basic_shape():
    twiml = build_conference_twiml(conference_name="cs-wt-CA123")
    assert twiml.startswith("<?xml")
    assert "<Dial>" in twiml
    assert "<Conference" in twiml
    assert ">cs-wt-CA123</Conference>" in twiml


def test_conference_twiml_default_flags():
    """Defaults: start_on_enter=True (target), end_on_exit=False
    (caller stays if target drops)."""
    twiml = build_conference_twiml(conference_name="room-1")
    assert 'startConferenceOnEnter="true"' in twiml
    assert 'endConferenceOnExit="false"' in twiml


def test_conference_twiml_caller_leg_waits():
    """The caller's leg is redirected with start_on_enter=False so they
    hear hold music until the transfer target joins."""
    twiml = build_conference_twiml(
        conference_name="room-1", start_on_enter=False
    )
    assert 'startConferenceOnEnter="false"' in twiml


def test_conference_twiml_end_on_exit_true():
    twiml = build_conference_twiml(
        conference_name="room-1", end_on_exit=True
    )
    assert 'endConferenceOnExit="true"' in twiml


def test_conference_twiml_wait_url_attribute():
    twiml = build_conference_twiml(
        conference_name="room-1",
        wait_url="https://example.com/wait.xml?x=1&y=2",
    )
    assert 'waitUrl="' in twiml
    # & must be XML-escaped inside the attribute value.
    assert "&amp;" in twiml


def test_conference_twiml_requires_name():
    with pytest.raises(ValueError):
        build_conference_twiml(conference_name="")


def test_conference_twiml_escapes_name_contents():
    """Defence-in-depth: the conference name could theoretically come
    from a caller-supplied override; special chars must be escaped."""
    twiml = build_conference_twiml(conference_name="a<b>c&d")
    assert "<b>" not in twiml  # the literal <b> tag is gone
    assert "&amp;" in twiml
    assert "&lt;b&gt;" in twiml


# ── conference-join endpoint ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_conference_join_returns_twiml():
    """The endpoint returns a TwiML response that Twilio fetches when the
    warm-transfer target answers. It must be reachable without auth since
    Twilio calls it mid-call flow."""
    from backend.app.api.telephony import twilio_conference_join

    resp = await twilio_conference_join(
        conference_name="cs-wt-test", start_on_enter=True
    )
    assert resp.media_type == "application/xml"
    body = resp.body.decode("utf-8")
    assert "<Conference" in body
    assert "cs-wt-test" in body
    assert 'startConferenceOnEnter="true"' in body


@pytest.mark.asyncio
async def test_conference_join_respects_start_on_enter_false():
    from backend.app.api.telephony import twilio_conference_join

    resp = await twilio_conference_join(
        conference_name="r", start_on_enter=False
    )
    body = resp.body.decode("utf-8")
    assert 'startConferenceOnEnter="false"' in body
