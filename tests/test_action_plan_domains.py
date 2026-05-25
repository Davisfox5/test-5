"""Domain template registry — names, fallback, prompt-template fields."""

from __future__ import annotations

import pytest

from backend.app.services.action_plan.domains import REGISTRY, get
from backend.app.services.action_plan.domains.base import DomainTemplate


def test_registry_has_four_domains():
    assert set(REGISTRY.keys()) == {
        "sales",
        "customer_service",
        "it_support",
        "generic",
    }


def test_get_known_domain_returns_template():
    template = get("sales")
    assert isinstance(template, DomainTemplate)
    assert template.name == "sales"
    assert template.role == "sales rep"


def test_get_unknown_domain_falls_back_to_generic():
    template = get("not_a_real_domain")
    assert template.name == "generic"


def test_get_empty_string_falls_back_to_generic():
    template = get("")
    assert template.name == "generic"


@pytest.mark.parametrize("domain_name", ["sales", "customer_service", "it_support", "generic"])
def test_template_has_required_fields(domain_name):
    """Every template must populate the fields Call A/B/C interpolate."""
    t = get(domain_name)
    assert t.name == domain_name
    assert t.role  # non-empty
    assert t.tone
    assert t.tone_description
    assert t.customer_endpoint_archetype
    assert t.customer_endpoint_description


@pytest.mark.parametrize("domain_name", ["sales", "customer_service", "it_support"])
def test_named_domains_have_loop_in_examples(domain_name):
    """Named domains (not generic) ship with concrete loop-in role examples
    so Call A's prompt has something to anchor on. Generic intentionally
    has minimal hints."""
    t = get(domain_name)
    assert len(t.loop_in_role_examples) >= 3


def test_sales_template_includes_pricing_slot():
    """Sales template's output_slot_examples should include pricing vocab
    so cross-plan analytics converge on the same slot_key for a common
    concept. Sanity check that we keep the example list useful."""
    t = get("sales")
    slot_keys = [ex.slot_key for ex in t.output_slot_examples]
    assert any("pric" in k.lower() for k in slot_keys)


def test_customer_service_includes_refund_or_ticket_slot():
    t = get("customer_service")
    slot_keys = [ex.slot_key for ex in t.output_slot_examples]
    joined = " ".join(slot_keys).lower()
    assert "refund" in joined or "ticket" in joined


def test_it_support_includes_repro_or_root_cause():
    t = get("it_support")
    slot_keys = [ex.slot_key for ex in t.output_slot_examples]
    joined = " ".join(slot_keys).lower()
    assert "repro" in joined or "root_cause" in joined
