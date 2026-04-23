"""Tests for the unified plan catalog + apply_tier behaviour."""

from types import SimpleNamespace

from backend.app.plans import (
    LEGACY_TIER_ALIASES,
    PLANS,
    apply_tier,
    default_tier,
    get_tier,
    list_tiers,
    normalize_tier_key,
)


def test_all_tiers_have_sane_seat_counts():
    for spec in PLANS.values():
        assert spec.seat_limit >= 1
        assert spec.admin_seat_limit >= 1
        assert spec.admin_seat_limit <= spec.seat_limit, (
            f"{spec.key}: admin_seat_limit must be <= seat_limit"
        )


def test_default_tier_is_sandbox():
    assert default_tier().key == "sandbox"
    assert default_tier().seat_limit == 3
    assert default_tier().admin_seat_limit == 1


def test_tiers_are_strictly_monotonic_on_seats():
    order = ["sandbox", "starter", "growth", "enterprise"]
    seats = [PLANS[k].seat_limit for k in order]
    assert seats == sorted(seats)
    assert len(set(seats)) == len(seats)  # no duplicates


def test_list_tiers_preserves_order_and_shape():
    out = list_tiers()
    assert [t["key"] for t in out] == list(PLANS.keys())
    required = {
        "key", "label", "description", "seat_limit", "admin_seat_limit",
        "features", "ai_model_tier",
    }
    for entry in out:
        assert required <= entry.keys()


def test_get_tier_falls_back_to_default():
    assert get_tier("does-not-exist").key == "sandbox"


def test_legacy_aliases_map_cleanly():
    assert normalize_tier_key("solo") == "sandbox"
    assert normalize_tier_key("team") == "starter"
    assert normalize_tier_key("pro") == "growth"
    assert normalize_tier_key("enterprise") == "enterprise"
    # Passing a modern key is a no-op.
    for k in PLANS:
        assert normalize_tier_key(k) == k
    # All legacy keys must resolve to a real current tier.
    for legacy, modern in LEGACY_TIER_ALIASES.items():
        assert modern in PLANS


def _fake_tenant(**overrides):
    t = SimpleNamespace(
        plan_tier="sandbox",
        seat_limit=1,
        admin_seat_limit=1,
        features_enabled={"live_kb_retrieval": True, "my_manual_flag": True},
    )
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


def test_apply_tier_sets_seat_limits():
    t = _fake_tenant()
    spec = apply_tier(t, "growth")
    assert t.plan_tier == "growth"
    assert t.seat_limit == spec.seat_limit
    assert t.admin_seat_limit == spec.admin_seat_limit


def test_apply_tier_accepts_legacy_key():
    t = _fake_tenant()
    spec = apply_tier(t, "pro")  # legacy → growth
    assert spec.key == "growth"
    assert t.plan_tier == "growth"


def test_apply_tier_merges_features_keeping_manual_flags():
    """Non-tier flags on the tenant must survive a tier change."""
    t = _fake_tenant(features_enabled={"my_manual_flag": True, "live_sentiment": False})
    apply_tier(t, "growth")  # growth turns live_sentiment on
    assert t.features_enabled["my_manual_flag"] is True
    assert t.features_enabled["live_sentiment"] is True


def test_apply_tier_unknown_falls_back_to_default():
    t = _fake_tenant(plan_tier="growth", seat_limit=50, admin_seat_limit=3)
    spec = apply_tier(t, "mystery-tier")
    assert spec.key == "sandbox"
    assert t.plan_tier == "sandbox"
    assert t.seat_limit == 3
    assert t.admin_seat_limit == 1
