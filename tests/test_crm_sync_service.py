"""Sync-service tests with a fake in-memory adapter + SQLAlchemy session.

We patch ``_build_adapter`` so the test doesn't need the real Integration
row or HubSpot/Salesforce keys, and we use a tiny FakeSession that records
upserts so we can assert idempotency and summary counts.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.crm.base import CrmContact, CrmCustomer
from backend.app.services.crm.sync_service import sync_crm_for_tenant


# ── Fakes ────────────────────────────────────────────────────────────


class FakeAdapter:
    provider = "hubspot"

    def __init__(self, customers: List[CrmCustomer], contacts: List[CrmContact]) -> None:
        self._customers = customers
        self._contacts = contacts

    async def iter_customers(self):
        for c in self._customers:
            yield c

    async def iter_contacts(self):
        for c in self._contacts:
            yield c

    async def close(self) -> None:
        return None


class FakeExecResult:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Minimal async-session double focused on sync_service's usage pattern.

    - ``add`` captures rows.
    - ``execute`` returns an object with ``scalar_one_or_none``; we dispatch
      on the statement's `.table.name` (works with SQLAlchemy Core selects).
    - ``flush`` assigns ids to newly-added Customer rows so the contact
      upsert path can resolve them.
    - ``get`` is unused by the code under test.
    """

    def __init__(self) -> None:
        self.added: List[Any] = []
        # Keep Customer rows here keyed by (tenant_id, crm_id) for
        # pseudo-idempotent lookups on repeat adds.
        self.customers: Dict[tuple, Any] = {}
        self.contacts: Dict[tuple, Any] = {}
        self.logs: List[Any] = []

    def add(self, obj) -> None:
        self.added.append(obj)
        cls = type(obj).__name__
        if cls == "Customer":
            if not getattr(obj, "id", None):
                obj.id = uuid.uuid4()
            self.customers[(obj.tenant_id, obj.crm_id)] = obj
        elif cls == "Contact":
            self.contacts[(obj.tenant_id, obj.crm_id, obj.crm_source)] = obj
        elif cls == "CrmSyncLog":
            if not getattr(obj, "id", None):
                obj.id = uuid.uuid4()
            self.logs.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, stmt) -> FakeExecResult:
        # Identify the target table off the SELECT. Good enough for the two
        # queries sync_service.py issues (Customer lookup + Contact lookup).
        try:
            table_name = stmt.get_final_froms()[0].name  # SA 2.x
        except Exception:
            table_name = ""
        if table_name == "customers":
            # Extract the crm_id param by walking clauses — simplest: iterate
            # all stored customers and pick the first matching one in
            # self.customers. The sync service always filters by tenant + crm_id.
            rows = [
                c for c in self.customers.values()
                if _matches(stmt, c, keys=("tenant_id", "crm_id"))
            ]
            return FakeExecResult(rows[:1])
        if table_name == "contacts":
            rows = [
                c for c in self.contacts.values()
                if _matches(stmt, c, keys=("tenant_id", "crm_id", "crm_source"))
            ]
            return FakeExecResult(rows[:1])
        return FakeExecResult([])

    async def get(self, model, key):
        return None


def _matches(stmt, candidate, keys):
    """Best-effort match — we compare the right-hand-side literals of the
    WHERE clause against the candidate row. It's okay for this to be
    approximate because the customer/contact dicts are keyed on the same
    fields the query filters on."""
    params = stmt.compile().params
    for k in keys:
        # SA names bind params like 'tenant_id_1', 'crm_id_1', etc.
        candidate_val = getattr(candidate, k, None)
        matched = False
        for pk, pv in params.items():
            if pk.startswith(k) and pv == candidate_val:
                matched = True
                break
        if not matched:
            return False
    return True


# ── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_inserts_customers_and_contacts_and_rebuilds_briefs():
    tenant_id = uuid.uuid4()
    customers = [
        CrmCustomer(external_id="101", name="Acme", domain="acme.com"),
        CrmCustomer(external_id="102", name="Globex"),
    ]
    contacts = [
        CrmContact(
            external_id="c1",
            name="Sarah Lee",
            email="sarah@acme.com",
            customer_external_id="101",
        ),
        CrmContact(
            external_id="c2",
            name="Hank Hill",
            email="hank@globex.com",
            customer_external_id="102",
        ),
    ]
    adapter = FakeAdapter(customers, contacts)
    db = FakeSession()

    scheduled: List[tuple] = []

    async def _fake_schedule(tid, cid):
        scheduled.append((tid, cid))

    with patch(
        "backend.app.services.crm.sync_service._build_adapter",
        AsyncMock(return_value=adapter),
    ), patch(
        "backend.app.services.crm.sync_service.schedule_customer_brief_rebuild",
        _fake_schedule,
    ):
        summary = await sync_crm_for_tenant(db, tenant_id, "hubspot")

    assert summary.status == "success"
    assert summary.customers_upserted == 2
    assert summary.contacts_upserted == 2
    assert summary.briefs_rebuilt == 2
    # Both new customers scheduled for a brief rebuild.
    assert len(scheduled) == 2


@pytest.mark.asyncio
async def test_sync_is_idempotent_on_rerun():
    """Second run on the same data should not insert duplicates or schedule
    more brief rebuilds (everyone was already in the DB)."""
    tenant_id = uuid.uuid4()
    customers = [CrmCustomer(external_id="101", name="Acme")]
    contacts = [
        CrmContact(
            external_id="c1",
            name="Sarah",
            email="sarah@acme.com",
            customer_external_id="101",
        )
    ]
    db = FakeSession()

    scheduled: List[tuple] = []

    async def _fake_schedule(tid, cid):
        scheduled.append((tid, cid))

    with patch(
        "backend.app.services.crm.sync_service._build_adapter",
        AsyncMock(side_effect=lambda *a, **k: FakeAdapter(customers, contacts)),
    ), patch(
        "backend.app.services.crm.sync_service.schedule_customer_brief_rebuild",
        _fake_schedule,
    ):
        first = await sync_crm_for_tenant(db, tenant_id, "hubspot")
        second = await sync_crm_for_tenant(db, tenant_id, "hubspot")

    assert first.briefs_rebuilt == 1
    assert second.briefs_rebuilt == 0  # no new customers → no extra rebuilds
    assert second.customers_upserted == 1  # we still process the row; just updates it
    # Total new Customer rows ever added = 1 (net-new insert only on first run).
    customers_added = [
        o for o in db.added if type(o).__name__ == "Customer"
    ]
    assert len(customers_added) == 1
