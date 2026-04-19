"""Pipedrive adapter — scaffold only.

Pipedrive supports OAuth but also a simpler ``api_token`` scheme that most
tenants use. Both land on the same GET endpoints, just with a different
auth mechanism. The adapter here is a skeleton that raises ``CrmError``
when called so integrators know to finish it — the sync service degrades
gracefully by skipping providers that raise ``NotImplementedError`` at
adapter construction time.
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

from backend.app.services.crm.base import (
    CrmContact,
    CrmCustomer,
    CrmError,
)


class PipedriveAdapter:
    provider = "pipedrive"

    def __init__(self, *args, **kwargs) -> None:
        # Explicit signal to the sync service: Pipedrive isn't implemented
        # yet. The endpoint handler surfaces a 501 Not Implemented.
        raise NotImplementedError(
            "Pipedrive adapter is not implemented yet — see services/crm/pipedrive.py"
        )

    async def iter_customers(self) -> AsyncIterator[CrmCustomer]:  # pragma: no cover
        raise CrmError("pipedrive: not implemented")
        yield  # makes this an async generator signature-wise

    async def iter_contacts(self) -> AsyncIterator[CrmContact]:  # pragma: no cover
        raise CrmError("pipedrive: not implemented")
        yield

    async def close(self) -> None:  # pragma: no cover
        return None
