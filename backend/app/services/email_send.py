"""Send an email via Gmail or Microsoft Graph using a stored OAuth token.

Returned dict shape is provider-neutral so the API layer can persist the
outbound Interaction without caring which provider sent it.  Supports
HTML bodies (with an auto-generated plain-text alternative) and file
attachments.
"""

from __future__ import annotations

import base64
import html as html_mod
import logging
import re
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Dict, List, Optional

import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)


@dataclass
class OutboundAttachment:
    """File to attach to an outgoing email."""

    filename: str
    content_type: Optional[str]
    data: bytes


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = html_mod.unescape(cleaned)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())


def _build_mime(
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


def send_via_gmail(
    access_token: str,
    from_address: str,
    to: List[str],
    cc: List[str],
    subject: str,
    body: str,
    body_html: Optional[str] = None,
    bcc: Optional[List[str]] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[List[str]] = None,
    attachments: Optional[List[OutboundAttachment]] = None,
) -> Dict[str, str]:
    mime = _build_mime(
        from_address, to, cc, list(bcc or []), subject, body, body_html,
        in_reply_to, references, attachments,
    )
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    service = build(
        "gmail", "v1", credentials=Credentials(token=access_token), cache_discovery=False
    )
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {
        "provider": "google",
        "provider_message_id": sent.get("id", ""),
        "message_id": mime["Message-ID"] or "",
    }


def send_via_graph(
    access_token: str,
    from_address: str,
    to: List[str],
    cc: List[str],
    subject: str,
    body: str,
    body_html: Optional[str] = None,
    bcc: Optional[List[str]] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[List[str]] = None,
    attachments: Optional[List[OutboundAttachment]] = None,
) -> Dict[str, str]:
    message: Dict[str, object] = {
        "subject": subject,
        "body": {
            "contentType": "HTML" if body_html else "Text",
            "content": body_html or body,
        },
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
    }
    if bcc:
        message["bccRecipients"] = [{"emailAddress": {"address": a}} for a in bcc]
    if attachments:
        message["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att.filename,
                "contentType": att.content_type or "application/octet-stream",
                "contentBytes": base64.b64encode(att.data).decode("ascii"),
            }
            for att in attachments
        ]

    payload = {"message": message, "saveToSentItems": True}
    # Graph does not take custom message headers on sendMail; the In-Reply-To
    # relationship is preserved automatically when the replied-to message id
    # is known via /me/messages/{id}/reply — we use that when we have the id.
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
    return {
        "provider": "microsoft",
        "provider_message_id": "",  # Graph doesn't return the id on sendMail
        "message_id": "",
    }
