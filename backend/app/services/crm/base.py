"""CRM adapter interface.

Each adapter turns a CRM's concept of accounts + people into our
``Customer`` + ``Contact`` row shapes. Adapters are async-friendly, paginated,
and surface authentication errors as ``CrmAuthError`` so the sync service
can pause that provider and surface a user-facing prompt to re-authorize.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, runtime_checkable


class CrmError(RuntimeError):
    """Base class for adapter failures."""


class CrmAuthError(CrmError):
    """Token invalid / refresh failed. Surfaced to the UI as re-auth."""


class CrmRateLimitError(CrmError):
    """Provider rate-limited us. The sync service should back off + retry."""


@dataclass
class CrmCustomer:
    """Neutral shape for a CRM account. ``external_id`` scopes within the
    provider (HubSpot company id, Salesforce Account.Id, etc.)."""

    external_id: str
    name: str
    domain: Optional[str] = None
    industry: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CrmContact:
    """Neutral shape for a CRM person."""

    external_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    # External id of the customer (account) this contact belongs to, if any.
    customer_external_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CrmAdapter(Protocol):
    """Contract every adapter implements. Iterators let us stream through
    long lists without buffering the whole dataset in memory."""

    provider: str

    async def iter_customers(self) -> AsyncIterator[CrmCustomer]:
        ...

    async def iter_contacts(self) -> AsyncIterator[CrmContact]:
        ...

    async def close(self) -> None:
        """Release any provider-side resources (HTTP client, etc.)."""
