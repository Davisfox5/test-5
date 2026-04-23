"""Unit tests for the A/B bucket routing logic — proves the consistency
guarantee the plan promises (a tenant always lands on the same variant)."""

from __future__ import annotations

import uuid

from backend.app.services import prompt_variant_service as svc


def test_bucket_is_deterministic_for_same_tenant() -> None:
    tenant_id = uuid.uuid4()
    surface = "analysis"
    bucket1 = svc._bucket(tenant_id, surface)
    bucket2 = svc._bucket(tenant_id, surface)
    assert bucket1 == bucket2


def test_bucket_varies_by_surface() -> None:
    tenant_id = uuid.uuid4()
    # We can't guarantee they differ for every tenant, but across a sample
    # the SHA-256 derived buckets should not collide universally.
    buckets = {svc._bucket(tenant_id, s) for s in ("analysis", "email_classifier", "email_reply")}
    assert len(buckets) >= 2


def test_bucket_distribution_uniform_at_scale() -> None:
    """Across 5,000 random tenants the bucket distribution should be ~uniform.

    Validates the routing won't systematically over- or under-allocate to
    shadow / canary / active.
    """
    counts = [0] * 100
    for _ in range(5000):
        tenant_id = uuid.uuid4()
        counts[svc._bucket(tenant_id, "analysis")] += 1
    # Each bucket expected ~50; allow generous bounds for a random sample.
    assert min(counts) > 10, f"low bucket {min(counts)} below expected floor"
    assert max(counts) < 120, f"high bucket {max(counts)} above expected ceiling"
