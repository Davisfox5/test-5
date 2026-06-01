"""Resolve LLM-emitted participants to real email addresses.

The analysis prompt emits a participant list with ``name``, ``role``,
``side`` ('customer' | 'vendor'), and ``source`` for each entry — but
no email. This service does best-effort name → email lookup against
the tenant's Contact and User tables and returns the enriched list.

Resolution is forgiving: when a name doesn't match cleanly, we keep
the participant with ``email=None``. The stub provider falls back to
name-only display; real calendar providers drop the un-resolved
participant from the invite (they need an email to send).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Contact, User
from backend.app.services.meeting_scheduler.base import MeetingParticipant

logger = logging.getLogger(__name__)


def _normalize(name: str) -> str:
    return " ".join(name.lower().strip().split())


def _name_matches(haystack: Optional[str], needle: str) -> bool:
    """Loose match: needle is a substring of haystack OR shares a token.

    The LLM might emit "Sarah" while the contact record is
    "Sarah Chen" — substring catches that. It might also emit "S.
    Chen" while the record is "Sarah Chen" — token-share catches that.
    Cheap and good enough; the rep can edit on the invite if wrong.
    """
    if not haystack:
        return False
    h = _normalize(haystack)
    n = _normalize(needle)
    if not h or not n:
        return False
    if n in h or h in n:
        return True
    h_tokens = set(h.split())
    n_tokens = set(n.split())
    return bool(h_tokens & n_tokens)


async def resolve_participants(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
    raw_participants: List[Dict[str, Any]],
) -> List[MeetingParticipant]:
    """Look up emails for each LLM-emitted participant entry.

    Returns ``MeetingParticipant`` instances ready for ``MeetingRequest``.
    Entries the LLM emitted with no usable name are dropped.
    """
    if not raw_participants:
        return []

    customer_contacts: List[Contact] = []
    if customer_id is not None:
        result = await db.execute(
            select(Contact).where(
                Contact.tenant_id == tenant_id,
                Contact.customer_id == customer_id,
            )
        )
        customer_contacts = list(result.scalars())

    # Vendor-side: tenant users. Most tenants have <50 users so loading
    # the whole set is cheap; keeps name matching in-process and fast.
    result = await db.execute(
        select(User).where(User.tenant_id == tenant_id)
    )
    tenant_users: List[User] = list(result.scalars())

    resolved: List[MeetingParticipant] = []
    for entry in raw_participants:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        side = (entry.get("side") or "").lower() or None
        role = entry.get("role")

        email: Optional[str] = None
        phone: Optional[str] = None
        if side == "customer":
            email, phone = _find_contact_match(customer_contacts, name)
        elif side == "vendor":
            email, phone = _find_contact_match(tenant_users, name)
        else:
            # Side unknown — try contacts first, then users.
            email, phone = _find_contact_match(customer_contacts, name)
            if not email and not phone:
                email, phone = _find_contact_match(tenant_users, name)

        resolved.append(
            MeetingParticipant(
                name=name, email=email, role=role, side=side, phone=phone,
            )
        )

    return resolved


def _find_contact_match(rows: List[Any], name: str) -> tuple[Optional[str], Optional[str]]:
    """Return (email, phone) for the first row whose name loosely matches.

    Either field can be None when the matched row doesn't carry it
    (most tenant ``User`` rows have email but not phone; some
    ``Contact`` rows are the reverse).
    """
    for row in rows:
        if _name_matches(getattr(row, "name", None), name):
            return (
                getattr(row, "email", None) or None,
                getattr(row, "phone", None) or None,
            )
    return (None, None)


def _find_email(rows: List[Any], name: str) -> Optional[str]:
    """Backward-compatible helper — returns just the email half of
    :func:`_find_contact_match` for any callers still on the old
    single-value shape."""
    email, _ = _find_contact_match(rows, name)
    return email
