"""Tests for the new customer-brief features: confidence scores + notes."""

from backend.app.services.kb.customer_brief_builder import (
    _empty_brief,
    _validate_brief,
    format_customer_brief_for_prompt,
)


def test_empty_brief_now_includes_field_confidences():
    brief = _empty_brief()
    assert "field_confidences" in brief
    assert brief["field_confidences"] == {}


def test_validate_clamps_confidence_scores():
    raw = {
        "overview": "hi",
        "field_confidences": {
            "overview": 0.8,
            "stakeholders": 1.5,   # out of range → clamp
            "best_approaches": -0.3,  # out of range → clamp
            "bad": "not a float",  # dropped silently
        },
    }
    out = _validate_brief(raw)
    conf = out["field_confidences"]
    assert conf["overview"] == 0.8
    assert conf["stakeholders"] == 1.0
    assert conf["best_approaches"] == 0.0
    assert "bad" not in conf


def test_validate_ignores_non_dict_field_confidences():
    raw = {"overview": "hi", "field_confidences": "oops"}
    out = _validate_brief(raw)
    assert out["field_confidences"] == {}


def test_format_customer_brief_still_renders_without_confidences():
    brief = _empty_brief()
    brief["overview"] = "Acme is a long-time customer."
    out = format_customer_brief_for_prompt(brief)
    assert "Acme is a long-time customer." in out
