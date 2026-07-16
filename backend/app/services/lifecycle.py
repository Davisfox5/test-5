"""Account lifecycle classification — single source of truth.

``lifecycle_stage`` decides whether an account is a ``"client"`` or a
``"prospect"``. Both the REST serializers (``api/contacts.py``) and the
outbound webhook emitters (``tasks.py``) gate churn-risk fields on this:
a prospect has no relationship to churn out of, so churn must never be
sent for one — on any surface. PR #176 gated the REST responses only;
the webhook payloads kept leaking churn for leads (the Flex console
rendered "5% churn" on prospects it ingested via webhooks).
"""

from __future__ import annotations

from typing import Any, Optional

STAGE_CLIENT = "client"
STAGE_PROSPECT = "prospect"


def lifecycle_stage(customer: Optional[Any]) -> str:
    """Classify an account as ``"client"`` or ``"prospect"``.

    An account is a client once it shows any post-sale signal — a
    ``renewal_date`` or an ``onboarding_status`` (both NULL until CRM
    sync or manual entry populates them; the schema never fabricates
    them). Everything else — including a missing/unresolved customer —
    is a prospect.
    """
    if customer is None:
        return STAGE_PROSPECT
    if (
        getattr(customer, "renewal_date", None) is not None
        or getattr(customer, "onboarding_status", None) is not None
    ):
        return STAGE_CLIENT
    return STAGE_PROSPECT


def is_client(customer: Optional[Any]) -> bool:
    return lifecycle_stage(customer) == STAGE_CLIENT
