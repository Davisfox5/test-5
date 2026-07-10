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


# ── Poller fetch_recent: a 404 on messages().get() must be per-message ──
# Regression for a wedged prod mailbox: history().list() returns a message
# id that 404s on get() (deleted/moved after the snapshot); the uncaught
# error crashed the whole poll before the cursor advanced, so every cycle
# re-hit the same dead id. fetch_recent must skip it and keep going.


class _FetchResp:
    def __init__(self, status):
        self.status = status
        self.reason = "Not Found"


class _RecentMessages:
    def __init__(self, http_error_cls):
        self._exc = http_error_cls  # gmail's actual HttpError class

    def list(self, userId, labelIds, maxResults):
        ids = {"INBOX": ["m1", "m2", "m3"], "SENT": ["m4"]}.get(labelIds[0], [])
        return _Req({"messages": [{"id": i} for i in ids]})

    def get(self, userId, id, format):
        if id == "m2":
            err = self._exc.__new__(self._exc)  # bypass __init__; fix reads .resp
            err.resp = _FetchResp(404)
            raise err
        return _Req({"id": id, "labelIds": ["SENT"] if id == "m4" else ["INBOX"]})


class _RecentService:
    def __init__(self, http_error_cls):
        self._messages = _RecentMessages(http_error_cls)

    def users(self):
        return self

    def messages(self):
        return self._messages


def test_fetch_recent_skips_404_message_not_fatal(monkeypatch):
    # Install the fake googleapiclient first so gmail's module-level imports
    # resolve even where the real lib isn't present (local sandbox).
    _install_fake_googleapiclient(monkeypatch, object())

    from backend.app.services.email_ingest import gmail as gmail_fetcher

    # Raise gmail's ACTUAL HttpError class so its `except HttpError` catches it.
    service = _RecentService(gmail_fetcher.HttpError)
    monkeypatch.setattr(gmail_fetcher, "build", lambda *a, **k: service)
    monkeypatch.setattr(gmail_fetcher, "build_credentials", lambda *a, **k: None)
    monkeypatch.setattr(
        gmail_fetcher,
        "normalize_message",
        lambda raw, agent_email, direction, service=None: types.SimpleNamespace(
            provider_message_id=raw["id"], direction=direction
        ),
    )

    out = list(
        gmail_fetcher.fetch_recent(
            integration=types.SimpleNamespace(provider="google"),
            cursor=None,  # first-run path → _recent_message_ids + get loop
            access_token="tok",
            agent_email="agent@example.com",
        )
    )
    # m2 (the 404) is skipped; the rest of INBOX and all of SENT arrive.
    assert [e.provider_message_id for e in out] == ["m1", "m3", "m4"]
