"""Tests for the ModelRouter capability additions that let every touchpoint
route through the router: explicit tier pinning, messages/tools passthrough,
telemetry folded into the router, and a streaming path with open-failover.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.services import llm_telemetry, model_catalog
from backend.app.services.model_router import (
    LLMRequest,
    ModelRouter,
    TaskType,
    Tier,
)


def _router_with(client):
    r = ModelRouter.__new__(ModelRouter)
    r._client = client  # type: ignore[attr-defined]
    return r


def _req(**kw) -> LLMRequest:
    defaults = dict(task_type=TaskType.MAIN_ANALYSIS, user_message="hi")
    defaults.update(kw)
    return LLMRequest(**defaults)


# ── forced tier ───────────────────────────────────────────────────────────


def test_forced_tier_overrides_task_selection():
    r = _router_with(None)
    # ORCH_CLIENT would normally be OPUS; a pinned tier wins.
    req = _req(task_type=TaskType.ORCH_CLIENT, forced_tier=Tier.HAIKU)
    assert r.select_tier(req) == Tier.HAIKU


def test_no_forced_tier_keeps_existing_behavior():
    r = _router_with(None)
    assert r.select_tier(_req(task_type=TaskType.TRIAGE)) == Tier.HAIKU
    assert r.select_tier(_req(task_type=TaskType.ORCH_WEEKLY)) == Tier.OPUS


# ── messages / tools passthrough + telemetry (ainvoke) ────────────────────


class _Resp:
    def __init__(self, model):
        self.model = model
        self.stop_reason = "end_turn"

        class _B:
            text = "ok"

        self.content = [_B()]
        self.usage = None


class _CapMessages:
    def __init__(self):
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _Resp(model=kwargs["model"])


class _CapClient:
    def __init__(self):
        self.messages = _CapMessages()


def test_ainvoke_passes_messages_and_tools_and_pins_model():
    client = _CapClient()
    r = _router_with(client)
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"}]
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]
    resp = asyncio.run(r.ainvoke(_req(forced_tier=Tier.SONNET, messages=msgs, tools=tools)))
    kw = client.messages.last_kwargs
    assert kw["model"] == model_catalog.SONNET
    assert kw["messages"] == msgs           # custom history used, not user_message
    assert kw["tools"] == tools             # tools forwarded
    assert resp.tier == Tier.SONNET


def test_ainvoke_defaults_to_user_message_when_no_messages():
    client = _CapClient()
    r = _router_with(client)
    asyncio.run(r.ainvoke(_req(forced_tier=Tier.HAIKU, user_message="solo")))
    assert client.messages.last_kwargs["messages"] == [{"role": "user", "content": "solo"}]
    assert "tools" not in client.messages.last_kwargs


def test_ainvoke_records_telemetry_when_call_site_set(monkeypatch):
    captured = []
    monkeypatch.setattr(
        llm_telemetry, "record_llm_completion",
        lambda *a, **k: captured.append((a, k)),
    )
    client = _CapClient()
    r = _router_with(client)
    asyncio.run(r.ainvoke(_req(forced_tier=Tier.HAIKU, call_site="kb_classifier", max_tokens=777)))
    assert len(captured) == 1
    args, _ = captured[0]
    assert args[0] == "kb_classifier"   # call_site
    assert args[1] == "haiku"           # tier
    assert args[2] == 777               # request_max_tokens


def test_ainvoke_no_telemetry_without_call_site(monkeypatch):
    captured = []
    monkeypatch.setattr(
        llm_telemetry, "record_llm_completion",
        lambda *a, **k: captured.append(a),
    )
    r = _router_with(_CapClient())
    asyncio.run(r.ainvoke(_req(forced_tier=Tier.HAIKU)))
    assert captured == []


# ── streaming path with open-failover (astream) ───────────────────────────


class _FakeStream:
    def __init__(self):
        self.events = ["e1", "e2"]

    async def __aiter__(self):
        for e in self.events:
            yield e


class _StreamCM:
    def __init__(self, model, fail):
        self.model = model
        self._fail = fail

    async def __aenter__(self):
        if self._fail:
            raise _Unavailable()
        return _FakeStream()

    async def __aexit__(self, *a):
        return False


class _Unavailable(Exception):
    status_code = 404


class _StreamMessages:
    def __init__(self, fail_models):
        self._fail_models = set(fail_models)
        self.opened = []

    def stream(self, **kwargs):
        model = kwargs["model"]
        self.opened.append(model)
        return _StreamCM(model, fail=model in self._fail_models)


class _StreamClient:
    def __init__(self, fail_models=()):
        self.messages = _StreamMessages(fail_models)


def test_astream_selects_model_and_yields_stream():
    client = _StreamClient()
    r = _router_with(client)

    async def run():
        req = _req(forced_tier=Tier.SONNET, messages=[{"role": "user", "content": "x"}],
                   tools=[{"name": "t"}])
        async with r.astream(req) as stream:
            got = [e async for e in stream]
        return got

    got = asyncio.run(run())
    assert got == ["e1", "e2"]
    assert client.messages.opened == [model_catalog.SONNET]


def test_astream_fails_over_when_primary_open_fails():
    # Opus open fails (404) → should fail over to Sonnet and succeed.
    client = _StreamClient(fail_models=[model_catalog.OPUS])
    r = _router_with(client)

    async def run():
        async with r.astream(_req(forced_tier=Tier.OPUS)) as stream:
            return [e async for e in stream]

    got = asyncio.run(run())
    assert got == ["e1", "e2"]
    assert client.messages.opened == [model_catalog.OPUS, model_catalog.SONNET]
