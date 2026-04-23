"""Gmail sender — sends email via the user's stored OAuth token.

Uses ``users/me/messages/send`` with a base64url-encoded RFC 822 message.
On 401 we run the refresh_token grant once and retry; a second 401 raises
``EmailAuthError`` so the UI can surface a re-auth button.
"""

from __future__ import annotations

import base64
import html as html_mod
import logging
import re
from email.message import EmailMessage
from typing import Callable, List, Optional, Union

import httpx

from backend.app.config import get_settings
from backend.app.services.email.base import (
    EmailAuthError,
    EmailSendError,
    OutboundAttachment,
    SendResult,
)

logger = logging.getLogger(__name__)

_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = html_mod.unescape(cleaned)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())


def _as_list(v: Union[str, List[str], None]) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


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
        to: Union[str, List[str]],
        subject: str,
        body: str,
        cc: Union[str, List[str], None] = None,
        bcc: Optional[List[str]] = None,
        body_html: Optional[str] = None,
        attachments: Optional[List[OutboundAttachment]] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[List[str]] = None,
    ) -> SendResult:
        mime = _build_mime(
            from_address=self._from,
            to=_as_list(to),
            cc=_as_list(cc),
            bcc=_as_list(bcc),
            subject=subject,
            body_text=body,
            body_html=body_html,
            in_reply_to=in_reply_to,
            references=references,
            attachments=attachments,
        )
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii").rstrip("=")

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
            message_id=mime["Message-ID"] or None,
            provider_message_id=data.get("id"),
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


def _build_mime(
    *,
    from_address: str,
    to: List[str],
    cc: List[str],
    bcc: List[str],
    subject: str,
    body_text: str,
    body_html: Optional[str],
    in_reply_to: Optional[str],
    references: Optional[List[str]],
    attachments: Optional[List[OutboundAttachment]],
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)

    msg.set_content(body_text or (_html_to_text(body_html) if body_html else ""))
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    for att in attachments or []:
        maintype, _, subtype = (att.content_type or "application/octet-stream").partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            att.data, maintype=maintype, subtype=subtype, filename=att.filename
        )
    return msg
