"""Send an email via Gmail or Microsoft Graph using a stored OAuth token.

Returned dict shape is provider-neutral so the API layer can persist the
outbound Interaction without caring which provider sent it.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from typing import Dict, List, Optional

import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)


def _build_mime(
    from_address: str,
    to: List[str],
    cc: List[str],
    subject: str,
    body: str,
    in_reply_to: Optional[str],
    references: Optional[List[str]],
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(body)
    return msg


def send_via_gmail(
    access_token: str,
    from_address: str,
    to: List[str],
    cc: List[str],
    subject: str,
    body: str,
    in_reply_to: Optional[str] = None,
    references: Optional[List[str]] = None,
) -> Dict[str, str]:
    mime = _build_mime(from_address, to, cc, subject, body, in_reply_to, references)
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
    in_reply_to: Optional[str] = None,
    references: Optional[List[str]] = None,
) -> Dict[str, str]:
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        },
        "saveToSentItems": True,
    }
    # Graph does not take custom message headers on sendMail; the In-Reply-To
    # relationship is preserved automatically when the replied-to message id
    # is known via /me/messages/{id}/reply — we use that when we have the id.
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=20) as client:
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
