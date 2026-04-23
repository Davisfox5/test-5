"""Email senders — Gmail + Outlook adapters for outbound email delivery.

Covers both post-call follow-up (``api/emails.py``) and conversation
replies (``api/conversations.py``). Supports plain text + HTML bodies,
attachments, BCC, and RFC 822 threading headers (Gmail).
"""

from backend.app.services.email.base import (
    EmailAuthError,
    EmailError,
    EmailSendError,
    EmailSender,
    OutboundAttachment,
    SendResult,
)
from backend.app.services.email.gmail import GmailSender
from backend.app.services.email.outlook import OutlookSender

__all__ = [
    "EmailAuthError",
    "EmailError",
    "EmailSendError",
    "EmailSender",
    "GmailSender",
    "OutboundAttachment",
    "OutlookSender",
    "SendResult",
]
