"""Tests for Linda chat's pre-rot history window.

`_load_history` replays a whole conversation each turn; without a cap, a long
chat grows context every turn (context rot + rising cost). `_window_history`
keeps the most-recent slice but only starts the window at a clean user turn so
the Anthropic contract (first message is a user turn, no dangling tool
exchange) still holds. Persistence is untouched — only what we SEND is trimmed.
"""

from __future__ import annotations

from backend.app.services.linda_agent import _window_history


def _u(text):
    return {"role": "user", "content": text}


def _a(text):
    return {"role": "assistant", "content": text}


def test_under_cap_returns_unchanged():
    msgs = [_u("hi"), _a("hello"), _u("more")]
    assert _window_history(msgs, max_messages=40) == msgs


def test_over_cap_trims_to_cap_and_starts_on_user():
    # 10 turns → 20 messages; cap at 6.
    msgs = []
    for i in range(10):
        msgs.append(_u(f"q{i}"))
        msgs.append(_a(f"a{i}"))
    out = _window_history(msgs, max_messages=6)
    assert len(out) <= 6
    assert out[0]["role"] == "user"  # Anthropic requires a user turn first
    # It must be a suffix of the original (recency window, order preserved).
    assert out == msgs[len(msgs) - len(out):]


def test_leading_assistant_is_dropped():
    # A naive last-N cut would start on an assistant message; window must drop it.
    msgs = [_u("q0"), _a("a0"), _u("q1"), _a("a1")]
    out = _window_history(msgs, max_messages=3)  # naive tail = [a0, q1, a1]
    assert out[0]["role"] == "user"
    assert out == [_u("q1"), _a("a1")]


def test_leading_tool_result_user_is_dropped():
    # Defensive: never start the window on a tool_result continuation.
    tool_result_msg = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "x", "content": "{}"}],
    }
    msgs = [_u("q0"), _a("a0"), tool_result_msg, _u("q1"), _a("a1")]
    out = _window_history(msgs, max_messages=3)  # naive tail = [tool_result, q1, a1]
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "q1"


def test_empty_history():
    assert _window_history([], max_messages=40) == []
