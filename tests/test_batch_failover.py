"""Tests for the Batches-path failover in ``ModelRouter.run_batch``.

The batch path is the non-interactive analogue of the live ``ainvoke`` path:
entries that come back errored with a *retryable* reason (transient overload /
timeout, or a model that was unavailable) are resubmitted ONCE on the fallback
tier (one step down, never up); deterministic client errors are left as-is.
These tests pin that contract plus the request-shaping consolidation (batch
entries inherit the temperature guard + Sonnet-5 thinking suppression) and the
sequential fallback used when the SDK lacks the Batches surface.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.services import llm_client, model_catalog
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


# ── Fake Batches SDK surface ──────────────────────────────────────────────


class _FakeBatch:
    def __init__(self, batch_id):
        self.id = batch_id
        self.processing_status = "ended"


class _FakeBatches:
    """Records each create() call's entries and returns scripted per-id results.

    ``results_by_batch`` maps the created batch index (0 = first submit,
    1 = failover round) to a ``{custom_id: result_entry}`` dict, where a result
    entry is either a success or an errored payload built by the helpers below.
    """

    def __init__(self, results_by_batch, *, raise_attribute=False):
        self._results_by_batch = results_by_batch
        self._raise_attribute = raise_attribute
        self.created_entries = []   # list[list[entry]] — one per create()

    async def create(self, *, requests):
        if self._raise_attribute:
            raise AttributeError("no batches surface")
        self.created_entries.append(requests)
        return _FakeBatch(f"batch-{len(self.created_entries) - 1}")

    async def retrieve(self, batch_id):
        return _FakeBatch(batch_id)

    async def results(self, batch_id):
        idx = int(batch_id.split("-")[1])
        entries = list(self._results_by_batch.get(idx, {}).values())

        async def _gen():
            for e in entries:
                yield e

        return _gen()


class _MessagesWithBatches:
    def __init__(self, batches, capture_create=None):
        self.batches = batches
        self._capture_create = capture_create

    async def create(self, **kwargs):
        # Used only by the sequential fallback path.
        if self._capture_create is not None:
            self._capture_create(kwargs)

        class _R:
            model = kwargs["model"]
            stop_reason = "end_turn"

            class _B:
                text = "seq-ok"

            content = [_B()]
            usage = None

        return _R()


class _BetaWrap:
    def __init__(self, messages):
        self.messages = messages


class _BatchClient:
    def __init__(self, batches, capture_create=None):
        msgs = _MessagesWithBatches(batches, capture_create)
        self.messages = msgs
        self.beta = _BetaWrap(msgs)


def _success(custom_id, text="ok"):
    return {
        "custom_id": custom_id,
        "result": {"type": "succeeded", "message": {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }},
    }


def _errored(custom_id, error_type):
    return {
        "custom_id": custom_id,
        "result": {"type": "errored", "error": {"type": error_type}},
    }


# ── Entry shaping ─────────────────────────────────────────────────────────


def test_batch_entry_omits_temperature_and_suppresses_thinking_for_sonnet5():
    # Batch entries must inherit the same guard the live path uses: Sonnet 5
    # 400s on temperature and defaults thinking on.
    r = _router_with(None)
    entry = r._batch_entry(_req(forced_tier=Tier.SONNET, temperature=0.0),
                           model_catalog.SONNET, "cid0")
    params = entry["params"]
    assert entry["custom_id"] == "cid0"
    assert params["model"] == model_catalog.SONNET
    assert "temperature" not in params
    assert params["thinking"] == {"type": "disabled"}
    assert "timeout" not in params   # client-side only, never a wire param


def test_batch_entry_keeps_temperature_for_haiku():
    r = _router_with(None)
    entry = r._batch_entry(_req(forced_tier=Tier.HAIKU, temperature=0.2),
                           model_catalog.HAIKU, "cid0")
    assert entry["params"]["temperature"] == 0.2
    assert "thinking" not in entry["params"]


def test_custom_id_prefers_metadata():
    r = _router_with(None)
    assert r._custom_id_for(_req(metadata={"custom_id": "abc"}), 3) == "abc"
    assert r._custom_id_for(_req(), 3) == "3"


# ── run_batch: happy path (no failover) ───────────────────────────────────


def test_run_batch_all_succeed_no_failover():
    batches = _FakeBatches({0: {"0": _success("0", "a"), "1": _success("1", "b")}})
    r = _router_with(_BatchClient(batches))
    out = asyncio.run(r.run_batch([_req(forced_tier=Tier.SONNET),
                                   _req(forced_tier=Tier.SONNET)]))
    assert out["0"]["text"] == "a"
    assert out["1"]["text"] == "b"
    assert len(batches.created_entries) == 1   # no second (failover) batch


# ── run_batch: per-entry failover ─────────────────────────────────────────


def test_run_batch_fails_over_retryable_entry_to_lower_tier():
    # Entry "1" overloads on the primary (opus) batch → resubmitted on sonnet.
    batches = _FakeBatches({
        0: {"0": _success("0", "a"), "1": _errored("1", "overloaded_error")},
        1: {"1": _success("1", "recovered")},
    })
    client = _BatchClient(batches)
    r = _router_with(client)
    out = asyncio.run(r.run_batch([_req(forced_tier=Tier.OPUS),
                                   _req(forced_tier=Tier.OPUS)]))
    assert out["0"]["text"] == "a"
    assert out["1"]["text"] == "recovered"   # replaced by the retry
    # Two batches submitted; the failover batch carried only the failed id on
    # the fallback (sonnet) model.
    assert len(batches.created_entries) == 2
    retry_entries = batches.created_entries[1]
    assert len(retry_entries) == 1
    assert retry_entries[0]["custom_id"] == "1"
    assert retry_entries[0]["params"]["model"] == model_catalog.SONNET


def test_run_batch_does_not_fail_over_deterministic_error():
    # invalid_request_error is the caller's bug — no retry, error preserved.
    batches = _FakeBatches({
        0: {"0": _errored("0", "invalid_request_error")},
    })
    r = _router_with(_BatchClient(batches))
    out = asyncio.run(r.run_batch([_req(forced_tier=Tier.OPUS)]))
    assert out["0"]["error"]["type"] == "invalid_request_error"
    assert len(batches.created_entries) == 1   # never resubmitted


def test_run_batch_no_failover_from_cheapest_tier():
    # Haiku has nowhere lower to go; a retryable error stays errored.
    batches = _FakeBatches({
        0: {"0": _errored("0", "overloaded_error")},
    })
    r = _router_with(_BatchClient(batches))
    out = asyncio.run(r.run_batch([_req(forced_tier=Tier.HAIKU)]))
    assert out["0"]["error"]["type"] == "overloaded_error"
    assert len(batches.created_entries) == 1


def test_run_batch_keeps_original_error_when_retry_also_fails():
    batches = _FakeBatches({
        0: {"0": _errored("0", "overloaded_error")},
        1: {"0": _errored("0", "overloaded_error")},
    })
    r = _router_with(_BatchClient(batches))
    out = asyncio.run(r.run_batch([_req(forced_tier=Tier.OPUS)]))
    assert out["0"]["error"]["type"] == "overloaded_error"
    assert len(batches.created_entries) == 2   # tried once, then gave up


def test_run_batch_model_unavailable_is_retryable():
    batches = _FakeBatches({
        0: {"0": _errored("0", "not_found_error")},
        1: {"0": _success("0", "recovered")},
    })
    r = _router_with(_BatchClient(batches))
    out = asyncio.run(r.run_batch([_req(forced_tier=Tier.OPUS)]))
    assert out["0"]["text"] == "recovered"


# ── run_batch: missing results are surfaced, not dropped ──────────────────


def test_run_batch_marks_missing_results_as_unavailable():
    # Poll timeout / fetch failure yields no entry for a submitted id. The id
    # must still appear in the result, errored — and must NOT trigger a
    # failover round (the original batch may still be running server-side).
    batches = _FakeBatches({0: {"0": _success("0", "a")}})   # "1" never arrives
    r = _router_with(_BatchClient(batches))
    out = asyncio.run(r.run_batch([_req(forced_tier=Tier.OPUS, metadata={"custom_id": "0"}),
                                   _req(forced_tier=Tier.OPUS, metadata={"custom_id": "1"})]))
    assert out["0"]["text"] == "a"
    assert out["1"]["error"]["type"] == "result_unavailable"
    assert len(batches.created_entries) == 1   # no resubmit for the missing id


# ── run_batch: failover round is best-effort ──────────────────────────────


def test_run_batch_returns_first_round_results_when_retry_submit_fails():
    # The failover round's own submit blowing up must not discard the first
    # round's (already paid-for) results.
    class _SecondCreateFails(_FakeBatches):
        async def create(self, *, requests):
            if self.created_entries:
                raise RuntimeError("boom")
            return await super().create(requests=requests)

    batches = _SecondCreateFails({
        0: {"0": _success("0", "a"), "1": _errored("1", "overloaded_error")},
    })
    r = _router_with(_BatchClient(batches))
    out = asyncio.run(r.run_batch([_req(forced_tier=Tier.OPUS, metadata={"custom_id": "0"}),
                                   _req(forced_tier=Tier.OPUS, metadata={"custom_id": "1"})]))
    assert out["0"]["text"] == "a"                          # kept
    assert out["1"]["error"]["type"] == "overloaded_error"  # original error intact


# ── run_batch: telemetry parity with the live path ────────────────────────


def test_run_batch_records_telemetry_per_successful_entry(monkeypatch):
    from backend.app.services import llm_telemetry

    recorded = []
    monkeypatch.setattr(
        llm_telemetry, "record_llm_completion",
        lambda call_site, tier, max_tokens, resp, tenant_id=None:
            recorded.append((call_site, tier, resp)),
    )
    # "0" succeeds on opus; "1" fails over and succeeds on sonnet; "2" stays
    # errored (deterministic) and must not be recorded.
    batches = _FakeBatches({
        0: {"0": _success("0", "a"), "1": _errored("1", "overloaded_error"),
            "2": _errored("2", "invalid_request_error")},
        1: {"1": _success("1", "recovered")},
    })
    r = _router_with(_BatchClient(batches))
    reqs = [_req(forced_tier=Tier.OPUS, call_site="batch_site", metadata={"custom_id": str(i)})
            for i in range(3)]
    asyncio.run(r.run_batch(reqs))
    assert [(cs, t) for cs, t, _ in recorded] == \
        [("batch_site", "opus"), ("batch_site", "sonnet")]
    # The recorded shim carries the served model so telemetry model slicing works.
    assert recorded[1][2]["model"] == model_catalog.SONNET


def test_run_batch_propagates_usage_from_dict_shaped_results():
    # The results iterator yields plain dicts on some SDK versions; usage must
    # survive extraction (_usage_dict getattr-only would drop it).
    batches = _FakeBatches({0: {"0": _success("0", "a")}})
    r = _router_with(_BatchClient(batches))
    out = asyncio.run(r.run_batch([_req(forced_tier=Tier.SONNET)]))
    assert out["0"]["usage"]["input_tokens"] == 1
    assert out["0"]["usage"]["output_tokens"] == 1


# ── run_batch: sequential fallback when the SDK lacks batches ──────────────


def test_run_batch_sequential_fallback_when_no_batches_surface():
    batches = _FakeBatches({}, raise_attribute=True)
    captured = []
    client = _BatchClient(batches, capture_create=lambda kw: captured.append(kw))
    r = _router_with(client)
    out = asyncio.run(r.run_batch([_req(forced_tier=Tier.SONNET, metadata={"custom_id": "x"}),
                                   _req(forced_tier=Tier.HAIKU, metadata={"custom_id": "y"})]))
    assert out["x"]["text"] == "seq-ok"
    assert out["y"]["text"] == "seq-ok"
    # Each request went through the live create() path once.
    assert len(captured) == 2


# ── submit-call transient retry ───────────────────────────────────────────


def test_create_batch_retries_transient_then_succeeds():
    class _Flaky:
        def __init__(self):
            self.calls = 0

        async def create(self, *, requests):
            self.calls += 1
            if self.calls == 1:
                raise llm_client.anthropic.InternalServerError.__new__(
                    llm_client.anthropic.InternalServerError
                )
            return _FakeBatch("batch-0")

    flaky = _Flaky()
    client = _BatchClient(_FakeBatches({}))
    client.beta.messages.batches = flaky

    r = _router_with(client)

    async def _run():
        return await r._create_batch([{"custom_id": "0", "params": {}}],
                                     _sleep=lambda _d: asyncio.sleep(0))

    batch_id = asyncio.run(_run())
    assert batch_id == "batch-0"
    assert flaky.calls == 2   # retried once


# ── error classifier ──────────────────────────────────────────────────────


def test_batch_error_is_retryable_classification():
    assert llm_client.batch_error_is_retryable("overloaded_error") is True
    assert llm_client.batch_error_is_retryable("not_found_error") is True
    assert llm_client.batch_error_is_retryable("rate_limit_error") is True
    assert llm_client.batch_error_is_retryable("invalid_request_error") is False
    assert llm_client.batch_error_is_retryable("authentication_error") is False
    assert llm_client.batch_error_is_retryable(None) is False
