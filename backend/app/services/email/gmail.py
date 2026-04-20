"""Gmail sender — sends a follow-up via the user's stored OAuth token.

Uses ``users/me/messages/send`` with a base64url-encoded RFC 822 message.
On 401 we run the refresh_token grant once and retry; a second 401 raises
``EmailAuthError`` so the UI can surface a re-auth button.
"""

from __future__ import annotations

import base64
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, Optional

import httpx

from backend.app.config import get_settings
from backend.app.services.email.base import (
    EmailAuthError,
    EmailSendError,
    SendResult,
)

logger = logging.getLogger(__name__)

_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GmailSender:
    provider = "google"

    def __init__(
        self,
        access_token: str,
        from_address: str,
        refresh_token: Optional[str] = None,
        on_token_refresh: Optional[Callable[..., "None"]] = None,
    ) -> None:
        if not access_token:
            raise EmailAuthError("Google access_token is required")
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._from = from_address
        self._on_token_refresh = on_token_refresh
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
    ) -> SendResult:
        raw = _build_raw_message(
            from_address=self._from,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
        )

        resp = await self._post_send(raw)
        if resp.status_code == 401 and self._refresh_token:
            await self._refresh_access_token()
            resp = await self._post_send(raw)
        if resp.status_code == 401:
            raise EmailAuthError("Gmail rejected the token after refresh")
        if resp.status_code >= 400:
            raise EmailSendError(
                f"Gmail send failed: {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json() or {}
        return SendResult(
            provider=self.provider,
            message_id=data.get("id"),
            raw_snippet=str(data)[:300],
        )

    async def _post_send(self, raw_rfc822: str) -> httpx.Response:
        return await self._client.post(
            _SEND_URL,
            json={"raw": raw_rfc822},
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
        )

    async def _refresh_access_token(self) -> None:
        settings = get_settings()
        client_id = settings.GOOGLE_CLIENT_ID
        client_secret = settings.GOOGLE_CLIENT_SECRET
        if not (client_id and client_secret and self._refresh_token):
            raise EmailAuthError("Google refresh credentials missing")
        resp = await self._client.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": self._refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise EmailAuthError(
                f"Google token refresh failed: {resp.status_code}"
            )
        body = resp.json()
        self._access_token = body.get("access_token") or self._access_token
        # Google sometimes omits refresh_token on subsequent refreshes; keep
        # the current one when absent.
        new_refresh = body.get("refresh_token")
        if new_refresh:
            self._refresh_token = new_refresh
        if self._on_token_refresh is not None:
            try:
                await self._on_token_refresh(
                    self._access_token,
                    self._refresh_token,
                    body.get("expires_in"),
                )
            except Exception:
                logger.exception("on_token_refresh callback failed")


def _build_raw_message(
    *, from_address: str, to: str, subject: str, body: str, cc: Optional[str] = None
) -> str:
    """Build a base64url-encoded RFC 822 message suitable for Gmail API."""
    # ``MIMEMultipart`` even for plain text keeps the API happy with
    # multipart-like senders; a single MIMEText part is fine.
    msg = MIMEMultipart("alternative")
    msg["From"] = from_address
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
