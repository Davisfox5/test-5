"""Credit-balance circuit breaker: open/probe/close lifecycle.

The breaker exists because an exhausted Anthropic balance is an EXPECTED
operational state here: it must pause all LLM calls, report once per
transition (never per occurrence), share state across workers via Redis,
and resume automatically once a probe succeeds.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import pytest

from backend.app.services import llm_circuit_breaker as breaker
from backend.app.services.llm_client import acreate_with_failover


class FakeRedis:
    """Just enough of redis-py for the breaker: get/set(nx, ex)/delete/exists."""

    def __init__(self):
        self.store: Dict[str, Any] = {}
        self.expiry: Dict[str, float] = {}

    def _expired(self, key: str) -> bool:
        exp = self.expiry.get(key)
        return exp is not None and time.time() >= exp

    def exists(self, key: str) -> int:
        if self._expired(key):
            self.store.pop(key, None)
            self.expiry.pop(key, None)
        return 1 if key in self.store else 0

    def set(self, key: str, value: Any, nx: bool = False, ex: Optional[int] = None):
        if nx and self.exists(key):
            return None
        self.store[key] = value
        if ex is not None:
            self.expiry[key] = time.time() + ex
        return True

    def delete(self, key: str) -> int:
        existed = 1 if key in self.store else 0
        self.store.pop(key, None)
        self.expiry.pop(key, None)
        return existed


class _CreditError(Exception):
    status_code = 400

    def __str__(self):
        return (
            "Error code: 400 - {'error': {'message': 'Your credit balance is "
            "too low to access the Anthropic API.'}}"
        )


class _OtherBadRequest(Exception):
    status_code = 400

    def __str__(self):
        return "Error code: 400 - {'error': {'message': 'max_tokens too large'}}"


@pytest.fixture()
def fake_breaker(monkeypatch):
    """Breaker wired to an in-memory Redis + a transition-report spy."""
    breaker._reset_for_tests()
    r = FakeRedis()
    monkeypatch.setattr(breaker, "_get_redis", lambda: r)
    reports = []
    monkeypatch.setattr(
        breaker, "_report_transition", lambda state, detail: reports.append(state)
    )
    yield r, reports
    breaker._reset_for_tests()


class _ProbeClient:
    """Client whose messages.create follows a script (exc instance or 'ok')."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

        outer = self

        class _Messages:
            async def create(self, **kwargs):
                outer.calls += 1
                step = outer._script.pop(0)
                if isinstance(step, Exception):
                    raise step
                return {"ok": True}

        self.messages = _Messages()


# ── classification ────────────────────────────────────────────────────


def test_is_credit_error_matches_the_billing_400_only():
    assert breaker.is_credit_error(_CreditError())
    assert not breaker.is_credit_error(_OtherBadRequest())

    class _FiveHundred(Exception):
        status_code = 500

    assert not breaker.is_credit_error(_FiveHundred())
    assert not breaker.is_credit_error(ValueError("credit balance is too low"))


# ── transitions report exactly once ───────────────────────────────────


def test_open_reports_once_across_repeated_failures(fake_breaker):
    _, reports = fake_breaker
    assert breaker.record_billing_failure(_CreditError()) is True
    assert breaker.record_billing_failure(_CreditError()) is True
    assert breaker.record_billing_failure(_CreditError()) is True
    assert reports == ["open"]
    assert breaker.is_open()


def test_non_credit_errors_never_open(fake_breaker):
    _, reports = fake_breaker
    assert breaker.record_billing_failure(_OtherBadRequest()) is False
    assert not breaker.is_open()
    assert reports == []


def test_close_reports_once(fake_breaker):
    _, reports = fake_breaker
    breaker.record_billing_failure(_CreditError())
    breaker.close()
    breaker.close()
    assert reports == ["open", "close"]
    assert not breaker.is_open()


# ── guard: pause, probe, resume ───────────────────────────────────────


def test_guard_noop_when_closed(fake_breaker):
    client = _ProbeClient([])
    asyncio.run(breaker.guard(client))
    assert client.calls == 0


def test_guard_raises_while_open_without_probe_slot(fake_breaker):
    breaker.record_billing_failure(_CreditError())
    # Open just set the probe key, so the slot is taken for a full interval.
    client = _ProbeClient(["ok"])
    with pytest.raises(breaker.LLMCallsSuspended):
        asyncio.run(breaker.guard(client))
    assert client.calls == 0


def test_probe_success_closes_and_lets_call_proceed(fake_breaker):
    r, reports = fake_breaker
    breaker.record_billing_failure(_CreditError())
    r.delete(breaker.PROBE_KEY)  # simulate the probe interval elapsing
    client = _ProbeClient(["ok"])
    asyncio.run(breaker.guard(client))  # must NOT raise
    assert client.calls == 1
    assert not breaker.is_open()
    assert reports == ["open", "close"]


def test_probe_credit_failure_keeps_open_quietly(fake_breaker):
    r, reports = fake_breaker
    breaker.record_billing_failure(_CreditError())
    r.delete(breaker.PROBE_KEY)
    client = _ProbeClient([_CreditError()])
    with pytest.raises(breaker.LLMCallsSuspended):
        asyncio.run(breaker.guard(client))
    assert breaker.is_open()
    assert reports == ["open"]  # no extra report per suppressed occurrence


# ── integration with acreate_with_failover ────────────────────────────


class _FailoverClient:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

        outer = self

        class _Messages:
            async def create(self, **kwargs):
                outer.calls.append(kwargs.get("model"))
                step = outer._script.pop(0)
                if isinstance(step, Exception):
                    raise step
                return step

        self.messages = _Messages()


def test_failover_wrapper_opens_breaker_and_raises_suspended(fake_breaker):
    _, reports = fake_breaker
    client = _FailoverClient([_CreditError()])
    with pytest.raises(breaker.LLMCallsSuspended):
        asyncio.run(acreate_with_failover(client, model="m", fallback_model="fb"))
    # No retry, no failover — billing is not transient.
    assert client.calls == ["m"]
    assert breaker.is_open()
    assert reports == ["open"]


def test_failover_wrapper_blocks_before_api_while_open(fake_breaker):
    breaker.record_billing_failure(_CreditError())
    client = _FailoverClient(["never-reached"])
    with pytest.raises(breaker.LLMCallsSuspended):
        asyncio.run(acreate_with_failover(client, model="m"))
    assert client.calls == []  # the API was never touched
