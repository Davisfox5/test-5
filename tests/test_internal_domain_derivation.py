"""Auto-derivation of a tenant's internal email domains from its users.

Guards the zero-config classification path: every tenant should get its
company domain(s) derived from its own users, minus public providers, so
the deterministic prefilter works without anyone setting anything.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.app.services.email_ingest.ingest import _tenant_domains


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, emails):
        self._emails = emails

    def query(self, *a, **k):
        return _FakeQuery([(e,) for e in self._emails])


def _tenant(configured=None):
    feats = {} if configured is None else {"email_internal_domains": configured}
    return SimpleNamespace(id="t1", features_enabled=feats)


def test_derives_company_domain_from_users():
    out = _tenant_domains(
        _tenant(), _FakeSession(["davis@flexonline.net", "amy@flexonline.net"])
    )
    assert out == ["flexonline.net"]


def test_public_providers_are_never_internal():
    out = _tenant_domains(
        _tenant(), _FakeSession(["owner@gmail.com", "x@outlook.com"])
    )
    assert out == []


def test_unions_configured_and_derived():
    out = _tenant_domains(
        _tenant(configured=["partner.com"]),
        _FakeSession(["davis@flexonline.net", "biz@gmail.com"]),
    )
    assert out == ["flexonline.net", "partner.com"]


def test_no_session_returns_configured_only():
    assert _tenant_domains(_tenant(configured=["flexonline.net"])) == ["flexonline.net"]


def test_result_is_memoized_on_tenant():
    t = _tenant()
    first = _tenant_domains(t, _FakeSession(["davis@flexonline.net"]))
    # A later call with different data returns the cached first result.
    assert _tenant_domains(t, _FakeSession(["other@acme.com"])) == first
