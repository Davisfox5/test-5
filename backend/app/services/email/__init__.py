"""Email senders — Gmail + Outlook adapters for follow-up email delivery."""

from backend.app.services.email.base import EmailAuthError, EmailSendError, EmailSender
from backend.app.services.email.gmail import GmailSender
from backend.app.services.email.outlook import OutlookSender

__all__ = [
    "EmailAuthError",
    "EmailSendError",
    "EmailSender",
    "GmailSender",
    "OutlookSender",
]
