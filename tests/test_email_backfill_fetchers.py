"""Regression tests for the backfill window fetchers.

The poison-message case: ``normalize_message`` raising on one malformed
payload must skip that message, not escape the generator. An escaping
exception fails the whole job — and because the message was never
ingested, it is absent from the dedupe index, so every retry dies on
the same message with no forward progress possible.

Provider APIs are faked at the client boundary (``googleapiclient``
build / ``httpx.Client``); ``normalize_message`` is monkeypatched to
blow up on one specific message id.
"""

from __future__ import annotations

import sys
import types

import httpx

# When the real googleapiclient is installed (CI), import the gmail
# module up front so it binds the real library — the sys.modules fake
# below then only affects the lazy imports inside fetch_window_gmail,
# not the cached module other tests share.
try:
    from backend.app.services.email_ingest import gmail as _gmail_preimport  # noqa: F401
except ImportError:
    pass


# ── Gmail fakes ──────────────────────────────────────────────────────


class _FakeHttpError(Exception):
    pass


class _Req:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeMessages:
    def __init__(self, folder_ids):
        self._folder_ids = folder_ids

    def list(self, userId, labelIds, q, maxResults, pageToken):
        ids = self._folder_ids.get(labelIds[0], [])
        return _Req({"messages": [{"id": i} for i in ids]})

    def get(self, userId, id, format):
        return _Req({"id": id, "payload": {"headers": []}})


class _FakeGmailService:
    def __init__(self, folder_ids):
        self._messages = _FakeMessages(folder_ids)

    def users(self):
        return self

    def messages(self):
        return self._messages


def _install_fake_googleapiclient(monkeypatch, service):
    """Provide googleapiclient whether or not it is installed; either
    way, ``build`` hands back our fake service."""
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda *args, **kwargs: service
    errors = types.ModuleType("googleapiclient.errors")
    errors.HttpError = _FakeHttpError
    pkg = types.ModuleType("googleapiclient")
    pkg.discovery = discovery
    pkg.errors = errors
    monkeypatch.setitem(sys.modules, "googleapiclient", pkg)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)
    monkeypatch.setitem(sys.modules, "googleapiclient.errors", errors)


def test_gmail_poison_message_is_skipped_not_fatal(monkeypatch):
    _install_fake_googleapiclient(
        monkeypatch,
        _FakeGmailService({"INBOX": ["m1", "m2", "m3"], "SENT": ["m4"]}),
    )

    from backend.app.services.email_ingest import backfill
    from backend.app.services.email_ingest import gmail as gmail_fetcher

    def _normalize(raw, agent_email, direction, service=None):
        if raw["id"] == "m2":
            raise ValueError("malformed MIME tree")
        return types.SimpleNamespace(
            provider_message_id=raw["id"], direction=direction
        )

    monkeypatch.setattr(gmail_fetcher, "normalize_message", _normalize)

    out = list(
        backfill.fetch_window_gmail(
            "tok", "agent@example.com", days=90, max_messages=10
        )
    )
    # m2 is dropped; the rest of INBOX and all of SENT still arrive.
    assert [e.provider_message_id for e in out] == ["m1", "m3", "m4"]


# ── Graph fakes ──────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeGraphClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        if "Inbox" in url:
            return _FakeResponse(
                {"value": [{"id": "g1"}, {"id": "g2"}, {"id": "g3"}]}
            )
        return _FakeResponse({"value": [{"id": "g4"}]})


def test_graph_poison_message_is_skipped_not_fatal(monkeypatch):
    from backend.app.services.email_ingest import backfill
    from backend.app.services.email_ingest import graph as graph_fetcher

    monkeypatch.setattr(httpx, "Client", _FakeGraphClient)

    def _normalize(raw, agent_email, direction, access_token=None):
        if raw["id"] == "g2":
            raise KeyError("emailAddress")
        return types.SimpleNamespace(
            provider_message_id=raw["id"], direction=direction
        )

    monkeypatch.setattr(graph_fetcher, "normalize_message", _normalize)

    out = list(
        backfill.fetch_window_graph(
            "tok", "agent@example.com", days=90, max_messages=10
        )
    )
    assert [e.provider_message_id for e in out] == ["g1", "g3", "g4"]
