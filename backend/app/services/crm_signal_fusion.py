"""Multi-source customer-candidate aggregation for entity resolution.

The plan called for fusing customer-name signals from "calls, transcripts,
chats, emails, and connected CRM contacts (Google, Microsoft, HubSpot,
Salesforce, Pipedrive, Zoho, MS Dynamics)". HubSpot / Salesforce /
Pipedrive are already synced *as ``Customer`` rows* by
``backend.app.services.crm.sync_service`` — those candidates show up
directly in the existing-customers query that
``entity_resolution._score_candidates`` runs first. So this module only
needs to cover the providers that are NOT yet writing to ``customers``:

- Google Contacts (organisations field)
- Microsoft Graph contacts (companyName field)
- Zoho CRM accounts
- MS Dynamics 365 accounts

When the corresponding adapters land (each will read live via the
existing ``Integration`` token, no DB sync needed) they hook into
:func:`gather_crm_candidates` here. For now this is a deliberate stub
that returns no extra candidates — the contract is stable, the
implementations slot in later without touching ``entity_resolution.py``.

Returning an empty list is safe: ``_score_candidates`` will fall back
to the existing-customers pool + the LLM's "create-new" candidate.
"""

from __future__ import annotations

import logging
import uuid
from typing import List

from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)


def gather_crm_candidates(
    *,
    session: Session,
    tenant_id: uuid.UUID,
) -> List["CustomerCandidate"]:  # noqa: F821 — forward-ref to avoid a cycle
    """Return CustomerCandidate objects from non-synced CRM sources.

    Stub today (returns []). Each provider adapter that lands will
    append its own candidates here, e.g.::

        from backend.app.services.google_contacts import iter_orgs

        for org in iter_orgs(session=session, tenant_id=tenant_id):
            yield CustomerCandidate(
                name=org.name,
                domain=org.domain,
                customer_id=None,
                crm_id=org.external_id,
                crm_source="google_contacts",
                source="crm_sync",
            )

    Why not call HubSpot / Salesforce / Pipedrive here? Because their
    sync service already writes ``Customer`` rows; entity_resolution's
    existing-customers query covers them. Adding them again here would
    double-count and bias the fuser toward CRM-sourced rows.
    """
    # Imported lazily to avoid a circular import at module load —
    # entity_resolution imports from this file too.
    from backend.app.services.entity_resolution import CustomerCandidate  # noqa: F401

    # When Google / Microsoft / Zoho / MS Dynamics adapters land, append
    # their candidates here. Each adapter is responsible for filtering
    # to the tenant's connected provider tokens via the Integration
    # table; this function should remain a thin aggregator.
    return []
