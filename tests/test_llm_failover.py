"""Tests for the bounded transient-retry + model-failover wrapper.

The wrapper is the one place a live LLM call gets resilience:
* transient errors (429 / 5xx / timeout) retry on the SAME model with backoff;
* a genuinely unavailable model (404 / deprecated) fails over to the cheaper
  tier's model exactly once;
* every retry/failover is logged with its reason;
* success passes straight through.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.services.llm_client import acreate_with_failover


class _Resp:
    def __init__(self, model):
        self.model = model


class _RateLimitError(Exception):
    status_code = 429


class _NotFoundError(Exception):
    status_code = 404


class _FakeMessages:
    def __init__(self, script):
        # script: list of either an exception instance (raise it) or a str (return _Resp(model))
        self._script = list(script)
        self.calls = []  # (model,) per attempt

    async def create(self, **kwargs):
        self.calls.append(kwargs["model"])
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return _Resp(model=kwargs["model"])


class _FakeClient:
    def __init__(self, script):
        self.messages = _FakeMessages(script)


async def _no_sleep(_):
    return None


def test_success_passes_through():
    client = _FakeClient(["primary-ok"])
    resp = asyncio.run(
        acreate_with_failover(client, model="primary", fallback_model="fb", _sleep=_no_sleep)
    )
    assert resp.model == "primary"
    assert client.messages.calls == ["primary"]


def test_transient_retries_same_model_then_succeeds(caplog):
    client = _FakeClient([_RateLimitError(), _RateLimitError(), "ok"])
    with caplog.at_level("WARNING"):
        resp = asyncio.run(
            acreate_with_failover(
                client, model="primary", fallback_model="fb", max_retries=2, _sleep=_no_sleep
            )
        )
    assert resp.model == "primary"
    # Retried on the SAME model (never failed over — all attempts primary).
    assert client.messages.calls == ["primary", "primary", "primary"]
    assert any("retry" in r.message.lower() for r in caplog.records)


def test_exhausted_transient_fails_over_to_fallback(caplog):
    # max_retries=1 → 2 primary attempts, both 429, then fail over to fb which succeeds.
    client = _FakeClient([_RateLimitError(), _RateLimitError(), "ok"])
    with caplog.at_level("WARNING"):
        resp = asyncio.run(
            acreate_with_failover(
                client, model="primary", fallback_model="fb", max_retries=1, _sleep=_no_sleep
            )
        )
    assert resp.model == "fb"
    assert client.messages.calls == ["primary", "primary", "fb"]
    assert any("failover" in r.message.lower() for r in caplog.records)


def test_model_unavailable_fails_over_immediately(caplog):
    client = _FakeClient([_NotFoundError(), "ok"])
    with caplog.at_level("WARNING"):
        resp = asyncio.run(
            acreate_with_failover(client, model="primary", fallback_model="fb", _sleep=_no_sleep)
        )
    assert resp.model == "fb"
    # No wasted retries on an unavailable model — straight to failover.
    assert client.messages.calls == ["primary", "fb"]
    assert any("unavailable" in r.message.lower() or "failover" in r.message.lower() for r in caplog.records)


def test_no_fallback_reraises_after_retries():
    client = _FakeClient([_RateLimitError(), _RateLimitError(), _RateLimitError()])
    with pytest.raises(_RateLimitError):
        asyncio.run(
            acreate_with_failover(
                client, model="primary", fallback_model=None, max_retries=2, _sleep=_no_sleep
            )
        )
