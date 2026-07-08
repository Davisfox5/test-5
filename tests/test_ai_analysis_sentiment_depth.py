"""Tests for the aspect-based sentiment upgrade in ``ai_analysis.py``.

The analyzer now emits two new fields alongside the existing coarse
``sentiment_overall`` bucket and ``sentiment_trajectory`` list:

* ``sentiment_score_direct`` — continuous 0-10 overall-call sentiment.
* ``sentiment_aspects`` — list of {aspect, valence, evidence_quote,
  confidence} for specific things the customer felt something about.

Parsing must be tolerant: a stubbed LLM response that omits either
field (or emits a malformed value) should never crash the pipeline —
it should land on the documented safe default instead.

Mirrors the ``ModelRouter.__new__`` stubbing pattern used in
``test_router_migration.py`` so we exercise ``AIAnalysisService.analyze``
end to end without a live API dependency.
"""
from __future__ import annotations

import asyncio
import json

import backend.app.services.ai_analysis as ai_analysis
from backend.app.services.ai_analysis import AIAnalysisService


class _FakeResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.text = text
        self.stop_reason = stop_reason


class _FakeRouter:
    """Stand-in for ``ModelRouter`` that returns a canned response."""

    def __init__(self, client=None, *, response_text: str):
        self._response_text = response_text

    async def ainvoke(self, request):
        return _FakeResponse(self._response_text)


def _make_service(monkeypatch, response_text: str) -> AIAnalysisService:
    monkeypatch.setattr(
        ai_analysis,
        "ModelRouter",
        lambda client=None: _FakeRouter(client, response_text=response_text),
    )
    service = AIAnalysisService.__new__(AIAnalysisService)
    service._client = None  # never touched — the fake router ignores it
    return service


_TRANSCRIPT = [{"time": "00:00", "speaker": "Rep", "text": "Hi there."}]


def test_analyze_parses_new_sentiment_fields_when_present(monkeypatch):
    stub = {
        "summary": "Customer happy with pricing, worried about migration.",
        "sentiment_overall": "mixed",
        "sentiment_trajectory": [{"time": "00:00", "score": 6.0}],
        "sentiment_score_direct": 7.3,
        "sentiment_aspects": [
            {
                "aspect": "pricing",
                "valence": 8.5,
                "evidence_quote": "The price is great for what we get.",
                "confidence": 0.9,
            },
            {
                "aspect": "migration effort",
                "valence": 3.0,
                "evidence_quote": "I'm worried about the cutover.",
                "confidence": 0.7,
            },
        ],
    }
    service = _make_service(monkeypatch, json.dumps(stub))
    result = asyncio.run(service.analyze(_TRANSCRIPT, tier="sonnet"))

    assert result["sentiment_score_direct"] == 7.3
    assert result["sentiment_aspects"] == [
        {
            "aspect": "pricing",
            "valence": 8.5,
            "evidence_quote": "The price is great for what we get.",
            "confidence": 0.9,
        },
        {
            "aspect": "migration effort",
            "valence": 3.0,
            "evidence_quote": "I'm worried about the cutover.",
            "confidence": 0.7,
        },
    ]
    # Unchanged legacy fields still pass through untouched.
    assert result["sentiment_overall"] == "mixed"
    assert result["sentiment_trajectory"] == [{"time": "00:00", "score": 6.0}]


def test_analyze_defaults_new_fields_when_model_omits_them(monkeypatch):
    stub = {
        "summary": "Routine call.",
        "sentiment_overall": "neutral",
        "sentiment_trajectory": [],
    }
    service = _make_service(monkeypatch, json.dumps(stub))
    result = asyncio.run(service.analyze(_TRANSCRIPT, tier="sonnet"))

    assert result["sentiment_score_direct"] is None
    assert result["sentiment_aspects"] == []
    # No crash, and the rest of the payload still comes through.
    assert result["summary"] == "Routine call."


def test_analyze_never_crashes_on_malformed_sentiment_depth_fields(monkeypatch):
    stub = {
        "summary": "Weird model output.",
        "sentiment_overall": "positive",
        "sentiment_score_direct": "very good",  # not a number
        "sentiment_aspects": [
            {"aspect": "pricing", "valence": 15.0},  # out of range -> dropped
            {"aspect": "", "valence": 5.0},  # blank aspect name -> dropped
            {"valence": 6.0},  # missing aspect -> dropped
            "not-a-dict",  # wrong type entirely -> dropped
            {
                "aspect": "support",
                "valence": 4.0,
                "confidence": "high",  # bad confidence -> defaults to 0.5
            },
        ],
    }
    service = _make_service(monkeypatch, json.dumps(stub))
    result = asyncio.run(service.analyze(_TRANSCRIPT, tier="sonnet"))

    assert result["sentiment_score_direct"] is None
    assert result["sentiment_aspects"] == [
        {
            "aspect": "support",
            "valence": 4.0,
            "evidence_quote": "",
            "confidence": 0.5,
        }
    ]


def test_apply_sentiment_depth_defaults_direct_unit():
    """Unit-level coverage of the tolerant-parsing helper itself."""
    from backend.app.services.ai_analysis import _apply_sentiment_depth_defaults

    result = {"sentiment_score_direct": -1.0, "sentiment_aspects": "not-a-list"}
    _apply_sentiment_depth_defaults(result)
    assert result["sentiment_score_direct"] is None
    assert result["sentiment_aspects"] == []

    result2 = {"sentiment_score_direct": 3.4}
    _apply_sentiment_depth_defaults(result2)
    assert result2["sentiment_score_direct"] == 3.4
    assert result2["sentiment_aspects"] == []
