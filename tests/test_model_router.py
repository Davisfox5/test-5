"""Tests for the :class:`ModelRouter` tier-selection rules and prompt caching.

We don't hit the Anthropic API here — we only verify the tier-selection
policy, the cache-control payload shape, and the helper functions that
build tenant/agent/client context blocks.
"""

from types import SimpleNamespace

import pytest

from backend.app.services.model_router import (
    CacheableBlock,
    LLMRequest,
    ModelRouter,
    TaskType,
    Tier,
    _build_system_payload,
    agent_profile_header,
    client_profile_header,
    tenant_context_block,
)


@pytest.fixture
def router():
    # No client instantiated — tier selection never touches Anthropic.
    r = ModelRouter.__new__(ModelRouter)
    r._client = None  # type: ignore[attr-defined]
    return r


def _req(**kw) -> LLMRequest:
    defaults = dict(
        task_type=TaskType.MAIN_ANALYSIS,
        user_message="",
        complexity_score=0.5,
        transcript_tokens=1000,
    )
    defaults.update(kw)
    return LLMRequest(**defaults)


def test_orchestrator_tasks_always_use_opus(router):
    for task in (
        TaskType.ORCH_CLIENT, TaskType.ORCH_AGENT,
        TaskType.ORCH_MANAGER, TaskType.ORCH_BUSINESS,
        TaskType.ORCH_WEEKLY, TaskType.QUALITY_REVIEW,
    ):
        assert router.select_tier(_req(task_type=task)) == Tier.OPUS


def test_triage_and_coaching_pick_use_haiku(router):
    assert router.select_tier(_req(task_type=TaskType.TRIAGE)) == Tier.HAIKU
    assert router.select_tier(_req(task_type=TaskType.COACHING_PICK)) == Tier.HAIKU


def test_delta_report_uses_sonnet(router):
    assert router.select_tier(_req(task_type=TaskType.DELTA_REPORT)) == Tier.SONNET


def test_main_analysis_routes_by_complexity(router):
    assert router.select_tier(_req(complexity_score=0.1)) == Tier.HAIKU
    assert router.select_tier(_req(complexity_score=0.5)) == Tier.SONNET
    assert router.select_tier(_req(complexity_score=0.9)) == Tier.SONNET


def test_main_analysis_bumps_for_large_transcripts(router):
    assert router.select_tier(
        _req(complexity_score=0.1, transcript_tokens=20000)
    ) == Tier.SONNET


def test_enterprise_tier_bumps_one_level(router):
    haiku_standard = router.select_tier(_req(complexity_score=0.1))
    haiku_enterprise = router.select_tier(_req(complexity_score=0.1, tenant_tier="enterprise"))
    assert haiku_standard == Tier.HAIKU
    assert haiku_enterprise == Tier.SONNET


def test_retry_count_bumps_one_level(router):
    base = router.select_tier(_req(complexity_score=0.5))
    retry = router.select_tier(_req(complexity_score=0.5, retry_count=1))
    assert retry.value != Tier.HAIKU.value  # either Sonnet or Opus
    assert retry == Tier.OPUS  # Sonnet + bump = Opus


def test_build_system_payload_marks_cacheable_blocks():
    payload = _build_system_payload([
        CacheableBlock(text="cached", cache=True),
        CacheableBlock(text="uncached", cache=False),
    ])
    assert payload[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload[1]


def test_build_system_payload_handles_empty_input():
    assert _build_system_payload([]) == []


def test_tenant_context_block_includes_canonical_glossary():
    tenant = SimpleNamespace(
        name="Acme Co",
        automation_level="suggest",
        canonical_glossary={"pricing": ["cost", "price", "fees"]},
    )
    block = tenant_context_block(tenant)
    assert "Acme Co" in block.text
    assert "pricing" in block.text
    assert block.cache is True


def test_agent_profile_header_strips_weak_skills_to_three():
    block = agent_profile_header({
        "summary": "Ramping new hire.",
        "metrics": {"weak_skills": ["a", "b", "c", "d", "e"]},
    })
    assert "a, b, c" in block.text
    assert "d" not in block.text  # capped to 3


def test_client_profile_header_handles_missing_history():
    block = client_profile_header({"summary": "Renewal account.", "history": []})
    assert "no prior deltas" in block.text
