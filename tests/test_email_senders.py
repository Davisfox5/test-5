"""Gmail + Outlook sender tests — covers message formatting, auth
refresh, and error-path mapping."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.email.base import EmailAuthError, EmailSendError
from backend.app.services.email.gmail import GmailSender, _build_raw_message
from backend.app.services.email.outlook import OutlookSender


def _http(status: int, body: dict) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        text=json.dumps(body),
        json=lambda: body,
    )


# ── Gmail ──────────────────────────────────────────────────────────────


def test_gmail_build_raw_message_is_base64url_rfc822():
    import email as _email

    raw = _build_raw_message(
        from_address="me@acme.com",
        to="sarah@foo.com",
        subject="Hey",
        body="Thanks for the call.",
        cc=None,
    )
    padded = raw + "=" * (-len(raw) % 4)
    decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")

    # Headers are plain text in the raw dump.
    assert "From: me@acme.com" in decoded
    assert "To: sarah@foo.com" in decoded
    assert "Subject: Hey" in decoded

    # MIME parts are base64-encoded inside — parse properly to assert body.
    msg = _email.message_from_bytes(base64.urlsafe_b64decode(padded))
    body_parts = [
        part.get_payload(decode=True).decode("utf-8")
        for part in msg.walk()
        if part.get_content_type() == "text/plain"
    ]
    assert any("Thanks for the call." in p for p in body_parts)


def test_gmail_build_raw_message_includes_cc_header():
    raw = _build_raw_message(
        from_address="me@acme.com",
        to="sarah@foo.com",
        subject="Hey",
        body="body",
        cc="manager@acme.com",
    )
    padded = raw + "=" * (-len(raw) % 4)
    decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    assert "Cc: manager@acme.com" in decoded


@pytest.mark.asyncio
async def test_gmail_send_success_returns_message_id():
    sender = GmailSender(access_token="at", from_address="me@acme.com")
    sender._client.post = AsyncMock(return_value=_http(200, {"id": "1877c"}))
    result = await sender.send(to="sarah@foo.com", subject="Hi", body="b")
    assert result.provider == "google"
    assert result.message_id == "1877c"


@pytest.mark.asyncio
async def test_gmail_send_401_without_refresh_raises_auth_error():
    sender = GmailSender(access_token="at", from_address="me@acme.com")
    sender._client.post = AsyncMock(return_value=_http(401, {"error": "invalid"}))
    with pytest.raises(EmailAuthError):
        await sender.send(to="s@x.com", subject="a", body="b")


@pytest.mark.asyncio
async def test_gmail_send_500_raises_send_error():
    sender = GmailSender(access_token="at", from_address="me@acme.com")
    sender._client.post = AsyncMock(return_value=_http(500, {"error": "boom"}))
    with pytest.raises(EmailSendError):
        await sender.send(to="s@x.com", subject="a", body="b")


@pytest.mark.asyncio
async def test_gmail_refresh_then_retry_path(monkeypatch):
    """401 on first attempt + refresh_token present → one refresh + one
    retry. Success on retry = sent."""
    # The refresh path reads client credentials off settings — patch them.
    from backend.app.services.email import gmail as gmail_mod

    monkeypatch.setattr(
        gmail_mod,
        "get_settings",
        lambda: SimpleNamespace(
            GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="sec"
        ),
    )

    sender = GmailSender(
        access_token="stale",
        refresh_token="ref",
        from_address="me@acme.com",
    )

    responses = iter(
        [
            _http(401, {"error": "expired"}),  # send attempt 1
            _http(200, {"access_token": "new", "refresh_token": "new-ref"}),  # refresh
            _http(200, {"id": "abc"}),  # send attempt 2
        ]
    )
    sender._client.post = AsyncMock(side_effect=lambda *a, **k: next(responses))
    result = await sender.send(to="s@x.com", subject="a", body="b")
    assert result.message_id == "abc"
    assert sender._access_token == "new"
    assert sender._refresh_token == "new-ref"


# ── Outlook ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outlook_send_success_on_202():
    sender = OutlookSender(access_token="at")
    sender._client.post = AsyncMock(return_value=_http(202, {}))
    result = await sender.send(to="s@x.com", subject="a", body="b")
    assert result.provider == "microsoft"
    # Graph's sendMail doesn't return a message id synchronously.
    assert result.message_id is None


@pytest.mark.asyncio
async def test_outlook_send_includes_cc_in_payload():
    sender = OutlookSender(access_token="at")

    captured = {}

    async def capture(url, json=None, headers=None):
        captured["json"] = json
        return _http(202, {})

    sender._client.post = AsyncMock(side_effect=capture)
    await sender.send(to="s@x.com", subject="a", body="b", cc="cc@x.com")
    recipients = captured["json"]["message"].get("ccRecipients")
    assert recipients and recipients[0]["emailAddress"]["address"] == "cc@x.com"


@pytest.mark.asyncio
async def test_outlook_401_without_refresh_raises_auth_error():
    sender = OutlookSender(access_token="at")
    sender._client.post = AsyncMock(return_value=_http(401, {"error": "invalid_grant"}))
    with pytest.raises(EmailAuthError):
        await sender.send(to="s@x.com", subject="a", body="b")


@pytest.mark.asyncio
async def test_outlook_non_202_raises_send_error():
    sender = OutlookSender(access_token="at")
    sender._client.post = AsyncMock(return_value=_http(500, {"error": "boom"}))
    with pytest.raises(EmailSendError):
        await sender.send(to="s@x.com", subject="a", body="b")


def test_outlook_requires_access_token():
    with pytest.raises(EmailAuthError):
        OutlookSender(access_token="")
