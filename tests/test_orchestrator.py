"""Tests for the orchestrator service — delta report writer, profile
store, and the daily/weekly cadence entry points.

We don't hit Claude or the database in these tests.  The router is
stubbed, and the profile store is tested against a mocked session so we
can assert the versioning behavior in isolation.
"""

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from backend.app.services.orchestrator import (
    DeltaReportWriter,
    ENTITY_AGENT,
    ENTITY_CLIENT,
    EntityScope,
    Orchestrator,
    ProfileEntityRef,
    ProfileStore,
    _append_history,
    _clamp01,
    _condense_llm,
)


# ── Helpers for async invocation without pytest-asyncio fixtures ──────────


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


# ── _condense_llm / _append_history / _clamp01 ───────────────────────────


def test_condense_llm_keeps_structured_signals_and_drops_summary():
    full = {
        "summary": "long prose we don't want in the orchestrator",
        "sentiment_overall": "positive",
        "sentiment_score": 7.5,
        "topics": [{"name": "pricing"}],
        "action_items": [{"title": "follow up"}],
        "_internal_cache_key": "should-not-appear",
    }
    condensed = _condense_llm(full)
    assert "summary" not in condensed
    assert condensed["sentiment_overall"] == "positive"
    assert condensed["topics"][0]["name"] == "pricing"
    assert "_internal_cache_key" not in condensed


def test_append_history_caps_at_ten_entries():
    history = [{"version": i, "headline": f"v{i}"} for i in range(15)]
    updated = _append_history(history, {"version": 16, "headline": "v16"})
    assert len(updated) == 10
    assert updated[0]["version"] == 16


def test_append_history_ignores_entries_with_no_version():
    history = [{"version": 1, "headline": "old"}]
    assert _append_history(history, {"version": 0}) == history


def test_clamp01_handles_strings_and_extremes():
    assert _clamp01(1.5) == 1.0
    assert _clamp01(-2.0) == 0.0
    assert _clamp01(None) is None
    assert _clamp01("not-a-number") is None


# ── Delta report writer ──────────────────────────────────────────────────


class _StubRouter:
    """Stub router that captures the request and returns a pre-baked response."""

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.last_request = None

    async def ainvoke(self, req):  # noqa: D401 — async stub
        self.last_request = req
        return SimpleNamespace(
            text=self.response_text,
            model="stub",
            tier="sonnet",
            stop_reason="end_turn",
            usage={},
            via_batch=False,
            parse_json=lambda: __import__("json").loads(self.response_text),
        )

    def invoke(self, req):
        self.last_request = req
        return SimpleNamespace(
            text=self.response_text,
            model="stub",
            tier="opus",
            stop_reason="end_turn",
            usage={},
            via_batch=False,
            parse_json=lambda: __import__("json").loads(self.response_text),
        )


def test_delta_report_writer_passes_features_and_parses_json():
    router = _StubRouter('{"client_delta": {"sentiment_shift": -0.5}}')
    writer = DeltaReportWriter(router=router)
    tenant = SimpleNamespace(name="Acme", automation_level="suggest", canonical_glossary={})
    interaction = SimpleNamespace(id=uuid.uuid4(), channel="voice", duration_seconds=120)
    features = {
        "deterministic": {"patience_sec": 0.8},
        "llm_structured": {"summary": "drop me", "sentiment_score": 7.2},
    }
    scopes = [EntityScope(entity_type=ENTITY_CLIENT, entity_id=str(uuid.uuid4()))]
    out = _run(writer.write(tenant=tenant, interaction=interaction, features=features, scopes=scopes))
    assert out["client_delta"]["sentiment_shift"] == -0.5
    # Summary was stripped before being sent to the router.
    assert "drop me" not in router.last_request.user_message


def test_delta_report_writer_returns_empty_on_invalid_json(caplog):
    router = _StubRouter("not json")
    writer = DeltaReportWriter(router=router)
    out = _run(writer.write(
        tenant=SimpleNamespace(name="X", automation_level="suggest", canonical_glossary={}),
        interaction=SimpleNamespace(id=uuid.uuid4(), channel="voice", duration_seconds=60),
        features={"deterministic": {}, "llm_structured": {}},
        scopes=[EntityScope(entity_type=ENTITY_AGENT, entity_id=str(uuid.uuid4()))],
    ))
    assert out == {}


# ── ProfileStore ─────────────────────────────────────────────────────────


@dataclass
class _FakeRow:
    id: uuid.UUID
    version: int
    profile: Dict[str, Any]
    top_factors: List[Any] = field(default_factory=list)
    confidence: float = 0.9
    created_at: Any = None


class _FakeSession:
    """In-memory substitute that just returns the highest-versioned row
    we've added.  Sufficient for the monotonic-version test because a
    single test only ever writes rows for one entity.
    """

    def __init__(self) -> None:
        self.rows: List[Any] = []

    def add(self, row: Any) -> None:
        row.id = uuid.uuid4()
        self.rows.append(row)

    def flush(self) -> None:
        pass

    def execute(self, stmt):
        latest = max(self.rows, key=lambda r: r.version, default=None)
        return MagicMock(scalar_one_or_none=lambda: latest)


def test_profile_store_assigns_monotonic_versions():
    session = _FakeSession()
    store = ProfileStore(session)
    contact_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ref = ProfileEntityRef(entity_type=ENTITY_CLIENT, entity_id=contact_id)

    first = store.append(
        ref=ref,
        tenant_id=tenant_id,
        profile={"summary": "first"},
        top_factors=[],
        source_event={"kind": "test"},
        confidence=0.8,
    )
    second = store.append(
        ref=ref,
        tenant_id=tenant_id,
        profile={"summary": "second"},
        top_factors=[],
        source_event={"kind": "test"},
        confidence=0.9,
    )
    assert first["version"] == 1
    assert second["version"] == 2


def test_profile_store_rejects_unknown_entity_type():
    session = _FakeSession()
    store = ProfileStore(session)
    with pytest.raises(ValueError):
        store.append(
            ref=ProfileEntityRef(entity_type="not-a-real-type", entity_id=uuid.uuid4()),
            tenant_id=uuid.uuid4(),
            profile={},
            top_factors=[],
            source_event={},
        )


# ── Orchestrator helpers ─────────────────────────────────────────────────


def test_orchestrator_record_delta_writes_a_row():
    orch = Orchestrator(router=_StubRouter("{}"))
    session = MagicMock()
    orch.record_delta(
        session,
        tenant_id=uuid.uuid4(),
        interaction_id=uuid.uuid4(),
        scopes=[EntityScope(entity_type=ENTITY_CLIENT, entity_id=str(uuid.uuid4()))],
        delta={"client_delta": {}},
    )
    session.add.assert_called_once()


# ── run_daily via the Batches path ────────────────────────────────────────


class _BatchStubRouter:
    """Stub whose ``run_batch`` returns pre-baked ``{custom_id: result}``."""

    def __init__(self, results=None, raise_on_submit=None):
        self.captured_requests = None
        self._results = results or {}
        self._raise = raise_on_submit

    async def run_batch(self, requests, **_kw):
        self.captured_requests = requests
        if self._raise is not None:
            raise self._raise
        return dict(self._results)


class _DailySession:
    """Fake session for run_daily: serves the tenant, the unconsumed deltas,
    an empty profile history, and records the consumed-at update."""

    def __init__(self, tenant, deltas):
        self._tenant = tenant
        self._deltas = deltas
        self.added = []
        self.consumed_update = None
        self.committed = False

    def get(self, _model, _pk):
        return self._tenant

    def add(self, row):
        row.id = uuid.uuid4()
        self.added.append(row)

    def flush(self):
        pass

    def commit(self):
        self.committed = True

    def execute(self, stmt):
        from sqlalchemy.sql.dml import Update

        from backend.app.models import DeltaReport

        if isinstance(stmt, Update):
            self.consumed_update = stmt
            return MagicMock()
        entity = stmt.column_descriptions[0]["entity"]
        m = MagicMock()
        if entity is DeltaReport:
            m.scalars.return_value.all.return_value = self._deltas
        else:  # ProfileStore.latest — no prior profile version
            m.scalar_one_or_none.return_value = None
        return m


def _delta_row(entity_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        interaction_id=uuid.uuid4(),
        created_at=None,
        delta={"client_delta": {"sentiment_shift": 0.1}},
        scopes=[{"type": ENTITY_CLIENT, "id": str(entity_id)}],
    )


_GOOD_PAYLOAD = (
    '{"summary": "s", "metrics": {}, "history_headline": "h",'
    ' "top_factors": [], "confidence": 0.5}'
)


def _batch_ok(text=_GOOD_PAYLOAD):
    return {"text": text, "usage": {}, "stop_reason": "end_turn"}


def _batch_err(error_type):
    return {"text": "", "usage": {}, "stop_reason": None,
            "error": {"type": error_type}}


def test_run_daily_submits_one_batch_and_consumes_on_success():
    tenant = SimpleNamespace(id=uuid.uuid4(), name="Acme",
                             automation_level="suggest", canonical_glossary={})
    e1, e2 = uuid.uuid4(), uuid.uuid4()
    session = _DailySession(tenant, [_delta_row(e1), _delta_row(e2)])
    router = _BatchStubRouter(results={
        f"client:{e1}": _batch_ok(),
        f"client:{e2}": _batch_ok(),
    })
    counts = Orchestrator(router=router).run_daily(session, tenant.id)

    assert counts == {ENTITY_CLIENT: 2}
    assert len(session.added) == 2                 # one profile version each
    assert session.consumed_update is not None     # deltas marked consumed
    assert session.committed
    # All entity buckets went out as ONE batch, telemetry-keyed.
    assert len(router.captured_requests) == 2
    for req in router.captured_requests:
        assert req.call_site == "orchestrator_daily"
        assert req.metadata["custom_id"].startswith("client:")


def test_run_daily_failed_entry_leaves_its_deltas_unconsumed():
    tenant = SimpleNamespace(id=uuid.uuid4(), name="Acme",
                             automation_level="suggest", canonical_glossary={})
    ok_id, bad_id = uuid.uuid4(), uuid.uuid4()
    session = _DailySession(tenant, [_delta_row(ok_id), _delta_row(bad_id)])
    router = _BatchStubRouter(results={
        f"client:{ok_id}": _batch_ok(),
        f"client:{bad_id}": _batch_err("overloaded_error"),
    })
    counts = Orchestrator(router=router).run_daily(session, tenant.id)

    assert counts == {ENTITY_CLIENT: 1}
    assert len(session.added) == 1                 # only the ok entity wrote
    assert session.consumed_update is not None     # ok bucket still consumed


def test_run_daily_submit_failure_consumes_nothing():
    tenant = SimpleNamespace(id=uuid.uuid4(), name="Acme",
                             automation_level="suggest", canonical_glossary={})
    session = _DailySession(tenant, [_delta_row(uuid.uuid4())])
    router = _BatchStubRouter(raise_on_submit=RuntimeError("submit down"))
    counts = Orchestrator(router=router).run_daily(session, tenant.id)

    assert counts == {}
    assert session.added == []
    assert session.consumed_update is None         # everything retries next run
    assert session.committed                       # commit is still a no-op close


def test_run_daily_parse_failure_consumes_but_writes_nothing():
    # Matches the sequential version: a malformed response is not retried —
    # the deltas are consumed, but no profile version is written.
    tenant = SimpleNamespace(id=uuid.uuid4(), name="Acme",
                             automation_level="suggest", canonical_glossary={})
    eid = uuid.uuid4()
    session = _DailySession(tenant, [_delta_row(eid)])
    router = _BatchStubRouter(results={f"client:{eid}": _batch_ok("not json")})
    counts = Orchestrator(router=router).run_daily(session, tenant.id)

    assert counts == {ENTITY_CLIENT: 1}
    assert session.added == []
    assert session.consumed_update is not None
