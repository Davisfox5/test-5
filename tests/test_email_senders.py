"""Gmail + Outlook sender tests — covers message formatting, auth
refresh, and error-path mapping."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.email.base import (
    EmailAuthError,
    EmailSendError,
    OutboundAttachment,
)
from backend.app.services.email.gmail import GmailSender, _build_mime
from backend.app.services.email.outlook import OutlookSender


def _http(status: int, body: dict) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        text=json.dumps(body),
        json=lambda: body,
    )


# ── Gmail ──────────────────────────────────────────────────────────────


def test_gmail_build_mime_produces_rfc822_with_headers_and_body():
    mime = _build_mime(
        from_address="me@acme.com",
        to=["sarah@foo.com"],
        cc=[],
        bcc=[],
        subject="Hey",
        body_text="Thanks for the call.",
        body_html=None,
        in_reply_to=None,
        references=None,
        attachments=None,
    )
    rendered = mime.as_string()
    assert "From: me@acme.com" in rendered
    assert "To: sarah@foo.com" in rendered
    assert "Subject: Hey" in rendered
    assert "Thanks for the call." in rendered


def test_gmail_build_mime_includes_cc_header():
    mime = _build_mime(
        from_address="me@acme.com",
        to=["sarah@foo.com"],
        cc=["manager@acme.com"],
        bcc=[],
        subject="Hey",
        body_text="body",
        body_html=None,
        in_reply_to=None,
        references=None,
        attachments=None,
    )
    assert "Cc: manager@acme.com" in mime.as_string()


def test_gmail_build_mime_adds_html_alternative_and_attachment():
    mime = _build_mime(
        from_address="me@acme.com",
        to=["sarah@foo.com"],
        cc=[],
        bcc=[],
        subject="Hey",
        body_text="plain",
        body_html="<p>rich</p>",
        in_reply_to="<prev@example.com>",
        references=["<prev@example.com>"],
        attachments=[
            OutboundAttachment(
                filename="invoice.pdf",
                content_type="application/pdf",
                data=b"%PDF-1.7\n...",
            )
        ],
    )
    rendered = mime.as_string()
    assert "In-Reply-To: <prev@example.com>" in rendered
    assert "References: <prev@example.com>" in rendered
    assert "invoice.pdf" in rendered
    # The HTML alternative part is present (content-type header).
    assert 'Content-Type: text/html' in rendered


@pytest.mark.asyncio
async def test_gmail_send_success_returns_provider_message_id():
    sender = GmailSender(access_token="at", from_address="me@acme.com")
    sender._client.post = AsyncMock(return_value=_http(200, {"id": "1877c"}))
    result = await sender.send(to="sarah@foo.com", subject="Hi", body="b")
    assert result.provider == "google"
    assert result.provider_message_id == "1877c"


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
    assert result.provider_message_id == "abc"
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
    assert result.provider_message_id is None


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
async def test_outlook_send_with_attachments_and_bcc():
    sender = OutlookSender(access_token="at")

    captured = {}

    async def capture(url, json=None, headers=None):
        captured["json"] = json
        return _http(202, {})

    sender._client.post = AsyncMock(side_effect=capture)
    await sender.send(
        to=["s@x.com"],
        subject="a",
        body="plain",
        body_html="<p>rich</p>",
        bcc=["archive@x.com"],
        attachments=[
            OutboundAttachment(filename="a.txt", content_type="text/plain", data=b"hi")
        ],
    )
    msg = captured["json"]["message"]
    assert msg["body"]["contentType"] == "HTML"
    assert msg["body"]["content"] == "<p>rich</p>"
    assert msg["bccRecipients"][0]["emailAddress"]["address"] == "archive@x.com"
    assert msg["attachments"][0]["name"] == "a.txt"
    assert (
        base64.b64decode(msg["attachments"][0]["contentBytes"]).decode() == "hi"
    )


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
