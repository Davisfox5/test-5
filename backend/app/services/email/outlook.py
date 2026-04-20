"""Outlook sender — Microsoft Graph ``/me/sendMail`` endpoint.

Accepts the same interface as ``GmailSender``. Token refresh uses the
Microsoft Identity platform's v2 token endpoint.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import httpx

from backend.app.config import get_settings
from backend.app.services.email.base import (
    EmailAuthError,
    EmailSendError,
    SendResult,
)

logger = logging.getLogger(__name__)

_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_DEFAULT_SCOPES = (
    "https://graph.microsoft.com/Mail.Send offline_access"
)


class OutlookSender:
    provider = "microsoft"

    def __init__(
        self,
        access_token: str,
        from_address: Optional[str] = None,
        refresh_token: Optional[str] = None,
        on_token_refresh: Optional[Callable[..., "None"]] = None,
    ) -> None:
        if not access_token:
            raise EmailAuthError("Microsoft access_token is required")
        self._access_token = access_token
        self._refresh_token = refresh_token
        # Microsoft Graph sends from the authenticated mailbox by default;
        # ``from_address`` is only used to render a From header when the
        # app is sending on behalf of another mailbox (rare). Safe to omit.
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
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }
        if cc:
            payload["message"]["ccRecipients"] = [
                {"emailAddress": {"address": cc}}
            ]

        resp = await self._post_send(payload)
        if resp.status_code == 401 and self._refresh_token:
            await self._refresh_access_token()
            resp = await self._post_send(payload)
        if resp.status_code == 401:
            raise EmailAuthError("Outlook rejected the token after refresh")
        # Graph returns 202 Accepted on successful send.
        if resp.status_code not in (200, 202):
            raise EmailSendError(
                f"Outlook send failed: {resp.status_code} {resp.text[:300]}"
            )
        return SendResult(
            provider=self.provider,
            # Graph's sendMail doesn't return a message id synchronously;
            # leave None and rely on the mailbox for traceability.
            message_id=None,
            raw_snippet=f"HTTP {resp.status_code}",
        )

    async def _post_send(self, payload: dict) -> httpx.Response:
        return await self._client.post(
            _SEND_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
        )

    async def _refresh_access_token(self) -> None:
        settings = get_settings()
        client_id = settings.MICROSOFT_CLIENT_ID
        client_secret = settings.MICROSOFT_CLIENT_SECRET
        if not (client_id and client_secret and self._refresh_token):
            raise EmailAuthError("Microsoft refresh credentials missing")
        resp = await self._client.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": self._refresh_token,
                "scope": _DEFAULT_SCOPES,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise EmailAuthError(
                f"Microsoft token refresh failed: {resp.status_code}"
            )
        body = resp.json()
        self._access_token = body.get("access_token") or self._access_token
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
