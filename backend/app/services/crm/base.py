"""CRM adapter interface.

Each adapter turns a CRM's concept of accounts + people into our
``Customer`` + ``Contact`` row shapes. Adapters are async-friendly, paginated,
and surface authentication errors as ``CrmAuthError`` so the sync service
can pause that provider and surface a user-facing prompt to re-authorize.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class CrmError(RuntimeError):
    """Base class for adapter failures.

    The ``transient`` flag tells :func:`retry_transient` whether the
    failure is worth retrying. Adapters set it to True for 5xx / network
    errors and False for 4xx user errors.
    """

    transient: bool = False


class CrmAuthError(CrmError):
    """Token invalid / refresh failed. Surfaced to the UI as re-auth."""

    transient: bool = False


class CrmRateLimitError(CrmError):
    """Provider rate-limited us. The sync service should back off + retry."""

    transient: bool = True


class CrmTransientError(CrmError):
    """Transient upstream failure (5xx, network blip). Eligible for retry."""

    transient: bool = True


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


# ── Shared retry helper ────────────────────────────────────────────
#
# Write-backs hit a remote CRM that may be flaky. We retry transient
# failures (5xx, network errors, 429) up to ``max_attempts`` times with
# exponential backoff; permanent failures (4xx other than 429) bubble
# immediately so callers don't waste time + log noise on user errors.


async def retry_transient(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
) -> Any:
    """Run ``fn`` and retry up to ``max_attempts`` on transient errors.

    Transient = ``CrmRateLimitError`` (provider rate-limited) or any
    ``CrmError`` whose ``transient`` attribute is truthy. ``CrmAuthError``
    is *not* retried — adapters refresh inline and a persistent auth
    failure means the tenant must re-authorize.

    Backoff is exponential with full jitter: ``min(max_delay,
    base_delay * 2 ** attempt) * random()``. ``sleep`` defaults to
    ``asyncio.sleep`` and is parametrised so tests can run without
    real waits.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    sleeper = sleep or asyncio.sleep
    last_exc: Optional[BaseException] = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except CrmAuthError:
            # Auth failures don't get retried at this layer — let the
            # adapter's inline refresh path handle them, and surface as
            # re-auth if that didn't work.
            raise
        except CrmRateLimitError as exc:
            last_exc = exc
        except CrmError as exc:
            if not getattr(exc, "transient", False):
                raise
            last_exc = exc

        # Don't sleep after the last attempt — bubble the failure.
        if attempt + 1 >= max_attempts:
            break
        delay = min(max_delay, base_delay * (2 ** attempt))
        # Full-jitter so a fleet of workers don't all retry at the same
        # tick after a provider blip.
        jittered = delay * (0.5 + random.random() * 0.5)
        await sleeper(jittered)

    assert last_exc is not None  # only reached when retries exhausted
    raise last_exc


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

    async def execute_operation(
        self,
        *,
        operation: str,
        payload: Dict[str, Any],
        contact_external_id: Optional[str] = None,
        customer_external_id: Optional[str] = None,
        deal_external_id: Optional[str] = None,
    ) -> str:
        """Generic dispatcher for synthesizer-emitted ``system_write``
        steps, where the LLM produces ``{operation: str, payload: {...}}``
        and we need to route it to the right concrete adapter method.

        The default implementation handles the operations the LLM is
        documented to emit (``create_task`` / ``create_activity`` /
        ``create_note`` / ``update_deal_stage``) by translating to the
        existing typed methods. Adapters that need provider-specific
        operations (custom objects, custom fields) override and add
        cases. Unknown operations raise CrmCapabilityMissing so the
        caller can surface a clear "this operation is not implemented
        on {provider}" message rather than a silent no-op.
        """
        op = (operation or "").lower().strip()
        if op in {"create_task", "create_activity"}:
            # LLM payload mirrors HubSpot's ``hs_task_*`` field names.
            # Map them to the activity_type / subject / note / due_date
            # the typed method expects. Any other key is preserved as
            # contextual ``note`` body.
            subject = (
                payload.get("subject")
                or payload.get("hs_task_subject")
                or payload.get("title")
                or "Task"
            )
            note = (
                payload.get("note")
                or payload.get("body")
                or payload.get("hs_task_body")
                or ""
            )
            due_date = (
                payload.get("due_date")
                or payload.get("hs_task_due_date")
                or payload.get("dueDate")
            )
            activity_type = payload.get("activity_type") or payload.get("type") or "task"
            return await self.create_activity(
                subject=str(subject),
                activity_type=str(activity_type),
                due_date=str(due_date) if due_date else None,
                note=str(note) if note else None,
                deal_external_id=deal_external_id,
                contact_external_id=contact_external_id,
            )
        if op == "create_note":
            content = (
                payload.get("content")
                or payload.get("body")
                or payload.get("hs_note_body")
                or ""
            )
            if not content:
                raise CrmError("create_note operation requires non-empty content")
            return await self.create_note(
                content=str(content),
                deal_external_id=deal_external_id,
                contact_external_id=contact_external_id,
                customer_external_id=customer_external_id,
            )
        if op == "update_deal_stage":
            deal_id = deal_external_id or payload.get("deal_external_id") or payload.get("deal_id")
            stage_id = payload.get("stage_external_id") or payload.get("stage_id") or payload.get("dealstage")
            if not deal_id or not stage_id:
                raise CrmError(
                    "update_deal_stage operation requires deal_external_id + stage_external_id"
                )
            await self.update_deal_stage(
                deal_external_id=str(deal_id),
                stage_external_id=str(stage_id),
            )
            return str(deal_id)
        raise CrmCapabilityMissing(
            f"{self.provider} adapter does not implement operation '{operation}'"
        )
