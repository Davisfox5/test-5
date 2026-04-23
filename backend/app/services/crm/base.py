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


@dataclass
class CrmDeal:
    """Neutral shape for a CRM sales opportunity (Pipedrive Deal,
    HubSpot Deal, Salesforce Opportunity). Amounts are in the CRM's
    native currency — no conversion here; downstream analytics convert
    using the tenant's reporting currency."""

    external_id: str
    title: str
    stage: Optional[str] = None
    status: Optional[str] = None  # open | won | lost | deleted
    amount: Optional[float] = None
    currency: Optional[str] = None
    probability: Optional[float] = None  # 0–1
    close_date: Optional[str] = None  # ISO-8601 date string
    customer_external_id: Optional[str] = None
    contact_external_id: Optional[str] = None
    owner_name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class CrmCapabilityMissing(CrmError):
    """Raised when a caller requests an adapter capability (deals,
    write-back, webhooks) the provider integration hasn't implemented.

    Catch at sync-service boundaries so one missing capability doesn't
    blow up the whole sync.
    """


@runtime_checkable
class CrmAdapter(Protocol):
    """Contract every adapter implements. Iterators let us stream through
    long lists without buffering the whole dataset in memory.

    Deals, note-creation, activity-creation, and deal-stage updates are
    optional. Adapters that don't support them should raise
    :class:`CrmCapabilityMissing` so the sync service can report a
    skipped-capability rather than an error.
    """

    provider: str

    async def iter_customers(self) -> AsyncIterator[CrmCustomer]:
        ...

    async def iter_contacts(self) -> AsyncIterator[CrmContact]:
        ...

    async def close(self) -> None:
        """Release any provider-side resources (HTTP client, etc.)."""

    # ── Optional write path (adapters may raise CrmCapabilityMissing) ──

    async def iter_deals(self) -> AsyncIterator[CrmDeal]:
        """Yield deals/opportunities. Default: capability missing."""
        raise CrmCapabilityMissing(f"{self.provider} adapter does not pull deals")
        yield  # pragma: no cover — makes this an async generator

    async def create_note(
        self,
        *,
        content: str,
        deal_external_id: Optional[str] = None,
        contact_external_id: Optional[str] = None,
        customer_external_id: Optional[str] = None,
    ) -> str:
        """Attach a note to a deal/contact/customer and return the
        provider's note id. Default: capability missing."""
        raise CrmCapabilityMissing(f"{self.provider} adapter does not create notes")

    async def create_activity(
        self,
        *,
        subject: str,
        activity_type: str,  # e.g., "call", "meeting", "task"
        due_date: Optional[str] = None,
        note: Optional[str] = None,
        deal_external_id: Optional[str] = None,
        contact_external_id: Optional[str] = None,
    ) -> str:
        """Create a follow-up activity and return its id. Default:
        capability missing."""
        raise CrmCapabilityMissing(
            f"{self.provider} adapter does not create activities"
        )

    async def update_deal_stage(
        self,
        *,
        deal_external_id: str,
        stage_external_id: str,
    ) -> None:
        """Move a deal to a new stage. Default: capability missing."""
        raise CrmCapabilityMissing(
            f"{self.provider} adapter does not update deal stages"
        )
