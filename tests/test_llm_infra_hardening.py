"""Challenge #3 residual gaps (docs/complexity/03-llm-infra.md) — written red-first.

G1: Linda chat history replay must reproduce the Anthropic wire shape for tool
    exchanges. Tool results are persisted as ``role="tool"`` rows; dropping them
    on replay leaves dangling ``tool_use`` blocks, which the API rejects (400)
    on every turn after a tool-using turn.
G2: The streaming path (``astream``) never records telemetry — the router only
    sees the request, the caller only sees the final message. The caller-side
    ``record_stream_completion`` closes the gap with the same served-tier
    reconciliation as ``ainvoke``.
G3: Fixed 2048 caps on Sonnet-5 surfaces (Ask Linda / email reply) with no
    truncation handling: caps go to 4096 via ``compute_max_tokens`` (learned
    ceilings apply) and truncation is detected — logged on Linda, flagged
    ``requires_human_review`` on email reply.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from backend.app.services import model_catalog
from backend.app.services.linda_agent import _rows_to_messages, _window_history


def _row(role, content="", tool_calls=None):
    return SimpleNamespace(role=role, content=content, tool_calls=tool_calls)


def _tool_use_blocks(tool_id="tu_1", text="let me check"):
    return [
        {"type": "text", "text": text},
        {"type": "tool_use", "id": tool_id, "name": "get_interactions", "input": {}},
    ]


def _tool_result_blocks(tool_id="tu_1"):
    return [{"type": "tool_result", "tool_use_id": tool_id, "content": "{}"}]


def _assert_wire_valid(messages):
    """Every tool_use is immediately answered; every tool_result is preceded by
    its tool_use — the Anthropic messages contract."""
    for i, msg in enumerate(messages):
        content = msg.get("content")
        use_ids = (
            {b["id"] for b in content if isinstance(b, dict) and b.get("type") == "tool_use"}
            if isinstance(content, list) else set()
        )
        result_ids = (
            {b["tool_use_id"] for b in content if isinstance(b, dict) and b.get("type") == "tool_result"}
            if isinstance(content, list) else set()
        )
        if msg["role"] == "assistant" and use_ids:
            assert i + 1 < len(messages), f"dangling tool_use at tail: {use_ids}"
            nxt = messages[i + 1]
            nxt_results = {
                b["tool_use_id"]
                for b in (nxt.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "tool_result"
            } if isinstance(nxt.get("content"), list) else set()
            assert nxt["role"] == "user" and use_ids == nxt_results, (
                f"tool_use {use_ids} not answered by next message"
            )
        if msg["role"] == "user" and result_ids:
            assert i > 0, "tool_result cannot open a conversation"
            prev = messages[i - 1]
            prev_uses = {
                b["id"]
                for b in (prev.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "tool_use"
            } if isinstance(prev.get("content"), list) else set()
            assert prev["role"] == "assistant" and result_ids == prev_uses, (
                f"orphan tool_result {result_ids}"
            )


# ── G1: faithful tool-exchange replay ──────────────────────────────────────


def test_tool_rows_replay_as_tool_result_user_turns():
    rows = [
        _row("user", "q"),
        _row("assistant", "let me check", _tool_use_blocks()),
        _row("tool", "", _tool_result_blocks()),
        _row("assistant", "answer"),
    ]
    msgs = _rows_to_messages(rows)
    assert msgs == [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": _tool_use_blocks()},
        {"role": "user", "content": _tool_result_blocks()},
        {"role": "assistant", "content": "answer"},
    ]
    _assert_wire_valid(msgs)


def test_replayed_tool_conversation_is_wire_valid_after_windowing():
    # 15 tool-using turns (user, assistant+tool_use, tool, assistant) = 60 rows;
    # the 40-message window must land on a clean user turn with no severed pair.
    rows = []
    for i in range(15):
        tid = f"tu_{i}"
        rows.extend([
            _row("user", f"q{i}"),
            _row("assistant", "checking", _tool_use_blocks(tid)),
            _row("tool", "", _tool_result_blocks(tid)),
            _row("assistant", f"answer {i}"),
        ])
    windowed = _window_history(_rows_to_messages(rows))
    assert windowed, "window must not be empty"
    assert windowed[0]["role"] == "user"
    assert not isinstance(windowed[0]["content"], list)
    _assert_wire_valid(windowed)


def test_dangling_tool_use_from_crashed_turn_is_stripped():
    # A turn that died between persisting the assistant tool_use and its tool
    # results must not poison every later turn: strip the tool_use, keep text.
    rows = [
        _row("user", "q"),
        _row("assistant", "let me check", _tool_use_blocks()),
        _row("user", "hello? you still there?"),
    ]
    msgs = _rows_to_messages(rows)
    _assert_wire_valid(msgs)
    assert msgs[1] == {"role": "assistant", "content": [{"type": "text", "text": "let me check"}]}


def test_tool_use_only_assistant_with_no_text_is_dropped():
    only_tool_use = [{"type": "tool_use", "id": "tu_9", "name": "x", "input": {}}]
    rows = [_row("user", "q"), _row("assistant", "", only_tool_use), _row("user", "next")]
    msgs = _rows_to_messages(rows)
    _assert_wire_valid(msgs)
    assert msgs == [{"role": "user", "content": "q"}, {"role": "user", "content": "next"}]


def test_orphan_tool_row_is_dropped():
    rows = [_row("user", "q"), _row("tool", "", _tool_result_blocks()), _row("assistant", "a")]
    msgs = _rows_to_messages(rows)
    _assert_wire_valid(msgs)
    assert msgs == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]


def test_empty_tool_row_is_skipped():
    rows = [_row("user", "q"), _row("tool", "", None), _row("assistant", "a")]
    assert _rows_to_messages(rows) == [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]


# ── G2: stream completions get recorded, with served-tier reconciliation ──


def _stream_req(**overrides):
    from backend.app.services.model_router import LLMRequest, TaskType, Tier

    kwargs = dict(
        task_type=TaskType.GENERIC,
        forced_tier=Tier.SONNET,
        user_message="",
        call_site="linda_chat",
        max_tokens=4096,
    )
    kwargs.update(overrides)
    return LLMRequest(**kwargs)


def _final_message(model, stop_reason="end_turn"):
    return SimpleNamespace(
        model=model,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def test_record_stream_completion_records_with_served_tier(monkeypatch):
    from backend.app.services import llm_telemetry
    from backend.app.services.model_router import ModelRouter

    calls = []
    monkeypatch.setattr(
        llm_telemetry, "record_llm_completion",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    router = ModelRouter(client=SimpleNamespace())
    # Request pinned to Sonnet, but the stream was actually served by Haiku
    # (open-time failover) — telemetry must report the SERVED tier.
    router.record_stream_completion(_stream_req(), _final_message(model_catalog.HAIKU))

    assert len(calls) == 1
    args, _ = calls[0]
    call_site, tier = args[0], args[1]
    assert call_site == "linda_chat"
    assert tier == "haiku"


def test_record_stream_completion_without_call_site_is_a_noop(monkeypatch):
    from backend.app.services import llm_telemetry
    from backend.app.services.model_router import ModelRouter

    calls = []
    monkeypatch.setattr(
        llm_telemetry, "record_llm_completion",
        lambda *args, **kwargs: calls.append(args),
    )
    router = ModelRouter(client=SimpleNamespace())
    router.record_stream_completion(
        _stream_req(call_site=None), _final_message(model_catalog.SONNET)
    )
    assert calls == []


# ── G3: email reply truncation flags human review ─────────────────────────


def test_truncated_email_reply_flags_human_review():
    from backend.app.services.email_reply import _draft_from_response

    response = SimpleNamespace(
        text='{"subject": "s", "body": "b", "rationale": "r", "citations": []}',
        stop_reason="max_tokens",
    )
    draft = _draft_from_response(response, SimpleNamespace(subject="Hello"))
    assert draft.requires_human_review is True
    assert draft.body == "b"


def test_clean_email_reply_is_not_flagged():
    from backend.app.services.email_reply import _draft_from_response

    response = SimpleNamespace(
        text='{"subject": "s", "body": "b", "rationale": "r", "citations": []}',
        stop_reason="end_turn",
    )
    draft = _draft_from_response(response, SimpleNamespace(subject="Hello"))
    assert draft.requires_human_review is False


def test_unparseable_email_reply_still_falls_back_to_raw():
    from backend.app.services.email_reply import _draft_from_response

    response = SimpleNamespace(text="definitely } not { json", stop_reason="end_turn")
    draft = _draft_from_response(response, SimpleNamespace(subject="Hello"))
    assert draft.requires_human_review is True
    assert draft.body == "definitely } not { json"


# ── G3 + G2 integration: one Linda turn through a fake router ─────────────


class _FakeStream:
    def __init__(self, final):
        self._final = final

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        return self._final


class _FakeRouter:
    def __init__(self, final):
        self._final = final
        self.requests = []
        self.recorded = []

    @asynccontextmanager
    async def astream(self, req):
        self.requests.append(req)
        yield _FakeStream(self._final)

    def record_stream_completion(self, req, final):
        self.recorded.append((req, final))


class _FakeDB:
    def __init__(self):
        self.added = []

    async def execute(self, stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _run_turn(monkeypatch, final):
    from backend.app.services import linda_agent

    fake = _FakeRouter(final)
    monkeypatch.setattr(linda_agent, "ModelRouter", lambda client: fake)
    ctx = linda_agent.AgentContext(
        db=_FakeDB(),
        tenant=SimpleNamespace(id=uuid.uuid4(), name="Acme", slug="acme"),
        user=SimpleNamespace(id=uuid.uuid4(), name="Dana", email="d@acme.io", role="admin"),
        conversation_id=uuid.uuid4(),
    )

    async def _go():
        events = []
        async for event in linda_agent.run_chat_turn(ctx, "hi"):
            events.append(event)
        return events

    return asyncio.run(_go()), fake


def test_linda_turn_uses_computed_cap_and_records_stream_telemetry(monkeypatch, caplog):
    final = SimpleNamespace(
        model=model_catalog.SONNET,
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="hello there")],
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    events, fake = _run_turn(monkeypatch, final)

    assert events[-1] == {"type": "done"}
    # Cap flows through compute_max_tokens (4096 override, sonnet ceiling 8192).
    assert fake.requests[0].max_tokens == 4096
    # The turn records its stream completion (G2 wiring).
    assert len(fake.recorded) == 1
    assert fake.recorded[0][1] is final


def test_linda_turn_logs_truncation(monkeypatch, caplog):
    final = SimpleNamespace(
        model=model_catalog.SONNET,
        stop_reason="max_tokens",
        content=[SimpleNamespace(type="text", text="cut off mid-")],
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=4096,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    with caplog.at_level(logging.WARNING, logger="backend.app.services.linda_agent"):
        events, fake = _run_turn(monkeypatch, final)

    assert events[-1] == {"type": "done"}
    assert any("max_tokens" in rec.message for rec in caplog.records)
