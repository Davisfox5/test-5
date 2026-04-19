"""Tests for the subscription-tier catalog + apply_tier behaviour."""

from types import SimpleNamespace

from backend.app.services.subscription_tiers import (
    SUBSCRIPTION_TIERS,
    apply_tier,
    default_tier,
    get_tier,
    list_tiers,
)


def test_all_tiers_have_sane_seat_counts():
    for spec in SUBSCRIPTION_TIERS.values():
        assert spec.seat_limit >= 1
        assert spec.admin_seat_limit >= 1
        assert spec.admin_seat_limit <= spec.seat_limit, (
            f"{spec.key}: admin_seat_limit must be <= seat_limit"
        )


def test_default_tier_is_solo():
    assert default_tier().key == "solo"
    assert default_tier().seat_limit == 1
    assert default_tier().admin_seat_limit == 1


def test_tiers_are_strictly_monotonic_on_seats():
    order = ["solo", "team", "pro", "enterprise"]
    seats = [SUBSCRIPTION_TIERS[k].seat_limit for k in order]
    assert seats == sorted(seats)
    assert len(set(seats)) == len(seats)  # no duplicates


def test_list_tiers_preserves_order_and_shape():
    out = list_tiers()
    assert [t["key"] for t in out] == list(SUBSCRIPTION_TIERS.keys())
    for entry in out:
        assert {"key", "label", "seat_limit", "admin_seat_limit", "features", "description"} <= entry.keys()


def test_get_tier_falls_back_to_default():
    assert get_tier("does-not-exist").key == "solo"


def _fake_tenant(**overrides):
    t = SimpleNamespace(
        subscription_tier="solo",
        seat_limit=1,
        admin_seat_limit=1,
        features_enabled={"live_kb_retrieval": True, "my_manual_flag": True},
    )
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


def test_apply_tier_sets_seat_limits():
    t = _fake_tenant()
    spec = apply_tier(t, "pro")
    assert t.subscription_tier == "pro"
    assert t.seat_limit == spec.seat_limit
    assert t.admin_seat_limit == spec.admin_seat_limit


def test_apply_tier_merges_features_keeping_manual_flags():
    """Non-tier flags on the tenant must survive a tier change."""
    t = _fake_tenant(features_enabled={"my_manual_flag": True, "live_sentiment": False})
    apply_tier(t, "pro")  # pro turns live_sentiment on
    assert t.features_enabled["my_manual_flag"] is True
    assert t.features_enabled["live_sentiment"] is True


def test_apply_tier_unknown_falls_back_to_default():
    t = _fake_tenant(subscription_tier="pro", seat_limit=50, admin_seat_limit=3)
    spec = apply_tier(t, "mystery-tier")
    assert spec.key == "solo"
    assert t.subscription_tier == "solo"
    assert t.seat_limit == 1
    assert t.admin_seat_limit == 1
