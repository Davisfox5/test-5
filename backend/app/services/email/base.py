"""Email sender interface.

Each provider returns a ``SendResult`` with the provider's message id when
available. Errors surface as ``EmailAuthError`` (re-authenticate) or
``EmailSendError`` (everything else), so the API layer can pick 401 vs 502.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


class EmailError(RuntimeError):
    """Generic send failure."""


class EmailAuthError(EmailError):
    """Token is bad / refresh failed. Surface as 401 to the client so the
    UI prompts a re-auth."""


class EmailSendError(EmailError):
    """Non-auth send failure (provider 4xx/5xx, transport error, etc.)."""


@dataclass
class SendResult:
    provider: str
    message_id: Optional[str]
    # Raw provider response body (trimmed) for the audit log.
    raw_snippet: str = ""


@runtime_checkable
class EmailSender(Protocol):
    provider: str

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
    ) -> SendResult: ...

    async def close(self) -> None: ...
