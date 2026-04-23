"""Tests for the audio storage service — local-filesystem backend path."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services import audio_storage
from backend.app.services.audio_storage import AudioStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIO_BUCKET", raising=False)
    monkeypatch.setenv("AUDIO_LOCAL_DIR", str(tmp_path))
    audio_storage._default_store = None
    return AudioStore()


def test_put_writes_audio_and_sidecar(store, tmp_path):
    handle = store.put(
        tenant_id="t1",
        interaction_id="i1",
        audio_bytes=b"RIFF...fake audio bytes",
        retention_hours=1,
    )
    assert handle.backend == "local"
    assert handle.local_path is not None
    assert os.path.exists(handle.local_path)
    assert os.path.exists(handle.local_path + ".meta")


def test_get_local_path_returns_same_file_for_local_backend(store):
    handle = store.put(
        tenant_id="t1", interaction_id="i1", audio_bytes=b"x", retention_hours=1
    )
    assert store.get_local_path(handle) == handle.local_path


def test_delete_removes_audio_and_sidecar(store):
    handle = store.put(
        tenant_id="t1", interaction_id="i1", audio_bytes=b"x", retention_hours=1
    )
    assert store.delete(handle) is True
    assert not os.path.exists(handle.local_path)
    assert not os.path.exists(handle.local_path + ".meta")


def test_sweep_expired_deletes_only_aged_objects(store, tmp_path):
    fresh = store.put(
        tenant_id="t1", interaction_id="fresh", audio_bytes=b"x", retention_hours=24
    )
    old = store.put(
        tenant_id="t1", interaction_id="old", audio_bytes=b"x", retention_hours=1
    )
    # Age the second object's sidecar by rewriting stored_at.
    old_meta = old.local_path + ".meta"
    with open(old_meta, "w") as fh:
        fh.write(
            "tenant_id=t1\n"
            "retention_hours=1\n"
            f"stored_at={(datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()}\n"
        )

    deleted = store.sweep_expired()
    assert deleted == 1
    assert os.path.exists(fresh.local_path)
    assert not os.path.exists(old.local_path)


def test_sweep_ignores_sidecars_with_missing_fields(store):
    handle = store.put(
        tenant_id="t1", interaction_id="bad", audio_bytes=b"x", retention_hours=1
    )
    # Corrupt the sidecar.
    with open(handle.local_path + ".meta", "w") as fh:
        fh.write("malformed\n")
    # Sweep should not raise and should not delete the file (can't prove
    # it's expired without usable metadata).
    assert store.sweep_expired() == 0
    assert os.path.exists(handle.local_path)
