"""Guard: the recommendation-category universe can't silently drift again.

The 2026-07 incident (Sentry LINDA-STAGING-2T): the DB CHECK constraint
kept the 4 original sales categories while the builder + detectors grew
13 more, so those INSERTs died with CheckViolation and poisoned the
builder's session. The constraint is now generated from
``models.MANAGER_RECOMMENDATION_CATEGORIES``; these tests fail the build
when any writer's category set escapes that tuple, or when the alembic
migration's copy of the list falls out of sync with the model's.
"""

from __future__ import annotations

import importlib.util
import os

from backend.app.models import MANAGER_RECOMMENDATION_CATEGORIES


def _load_sen_001():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backend",
        "alembic",
        "versions",
        "sen_001_reconcile_recommendation_drift.py",
    )
    spec = importlib.util.spec_from_file_location("sen_001", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_builder_whitelists_are_covered():
    from backend.app.services.manager_recommendation_builder import (
        _VALID_CATEGORIES_BY_DOMAIN,
    )

    union = set().union(*_VALID_CATEGORIES_BY_DOMAIN.values())
    missing = union - set(MANAGER_RECOMMENDATION_CATEGORIES)
    assert not missing, (
        "Builder categories missing from MANAGER_RECOMMENDATION_CATEGORIES "
        "(add them AND ship a migration recreating "
        "ck_manager_recommendations_category): %s" % sorted(missing)
    )


def test_detector_and_orchestrator_categories_are_covered():
    from backend.app.services.cs_trend_detector import (
        RECOMMENDATION_CATEGORY as CS_CATEGORY,
    )
    from backend.app.services.sales_trend_detector import (
        RECOMMENDATION_CATEGORY as SALES_CATEGORY,
    )

    allowed = set(MANAGER_RECOMMENDATION_CATEGORIES)
    assert CS_CATEGORY in allowed
    assert SALES_CATEGORY in allowed

    # support_trend_detector + cohort_recommendations + orchestrator
    # write these literals (see each module).
    for cat in (
        "address_recurring_issue",
        "prevent_no_touch_churn",
        "prevent_lead_stall",
        "proactive_outreach_repeat_support",
        "coach_rep",
        "coach_csm",
        "coach_support_agent",
    ):
        assert cat in allowed, cat


def test_migration_matches_model_tuple():
    mod = _load_sen_001()
    assert set(mod._CATEGORIES) == set(MANAGER_RECOMMENDATION_CATEGORIES)


def test_model_check_constraint_covers_the_tuple():
    from backend.app.models import ManagerRecommendation

    cks = [
        c
        for c in ManagerRecommendation.__table__.constraints
        if getattr(c, "name", None) == "ck_manager_recommendations_category"
    ]
    assert len(cks) == 1
    sql = str(cks[0].sqltext)
    for cat in MANAGER_RECOMMENDATION_CATEGORIES:
        assert "'%s'" % cat in sql
