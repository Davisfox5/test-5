"""The diarization Redis cache must be tenant-scoped.

Regression for the 4c audit finding: the key was ``diarization:{sha256}``
only, so two tenants uploading byte-identical audio shared the cached
speaker labels. The key now includes the bound RLS tenant context, and
with NO tenant bound there is no key at all — an unwired code path gets a
cache miss, never a cross-tenant hit.
"""

import uuid

from backend.app.services.transcription import _diarization_cache_key
from backend.app.tenant_ctx import tenant_context


def test_cache_key_includes_tenant():
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    audio_hash = "ab" * 32

    with tenant_context(tenant_a):
        key_a = _diarization_cache_key(audio_hash)
    with tenant_context(tenant_b):
        key_b = _diarization_cache_key(audio_hash)

    assert key_a == "diarization:{0}:{1}".format(tenant_a, audio_hash)
    assert key_a != key_b  # identical audio, different tenants → different keys


def test_cache_key_is_none_without_tenant_context():
    assert _diarization_cache_key("ab" * 32) is None
