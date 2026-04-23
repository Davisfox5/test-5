"""Email sender interface.

Each provider returns a ``SendResult`` with the provider's message id when
available. Errors surface as ``EmailAuthError`` (re-authenticate) or
``EmailSendError`` (everything else), so the API layer can pick 401 vs 502.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, runtime_checkable


class EmailError(RuntimeError):
    """Generic send failure."""


class EmailAuthError(EmailError):
    """Token is bad / refresh failed. Surface as 401 to the client so the
    UI prompts a re-auth."""


class EmailSendError(EmailError):
    """Non-auth send failure (provider 4xx/5xx, transport error, etc.)."""


@dataclass
class OutboundAttachment:
    """File to attach to an outgoing email."""

    filename: str
    content_type: Optional[str]
    data: bytes


@dataclass
class SendResult:
    provider: str
    message_id: Optional[str]
    # Provider-assigned id for the row we just created (Gmail only).
    provider_message_id: Optional[str] = None
    # Raw provider response body (trimmed) for the audit log.
    raw_snippet: str = ""


@runtime_checkable
class EmailSender(Protocol):
    provider: str

    async def send(
        self,
        *,
        to: List[str],
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        body_html: Optional[str] = None,
        attachments: Optional[List[OutboundAttachment]] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[List[str]] = None,
    ) -> SendResult: ...

    async def close(self) -> None: ...
