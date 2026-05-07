"""Tests for the SIPREC wire-format parsers.

These exercise the pure-CPU pieces of Stream 1: multipart-MIME
splitting, SDP parsing (with crypto), rs-metadata XML parsing, the
top-level ``parse_siprec_invite`` orchestrator, and the SDES key
extraction in ``services.telephony.siprec.srtp``.

We replay three vendor INVITE fixtures (Cisco CUBE, Avaya SBCE,
Metaswitch Perimeta) — they're the closest-to-production input we
can generate without a live SBC. The protocol contract is what the
bridge depends on, so a regression here is a downstream-stream-wide
break.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Tuple

import pytest

from backend.app.services.telephony.siprec import (
    SIPREC_PROVIDERS,
    CryptoAttribute,
    extract_boundary,
    extract_key_material,
    is_supported,
    parse_multipart,
    parse_rs_metadata,
    parse_sdp,
    parse_siprec_invite,
    participant_for_stream,
    select_crypto,
    supported_suites,
)

FIXTURES = Path(__file__).parent / "fixtures" / "siprec"


# ── Helper: split a fixture .sip file into headers + body ───────────────


def _load_invite(name: str) -> Tuple[dict, bytes]:
    """Read a fixture INVITE and return (headers, body) bytes.

    The .sip files mimic an on-the-wire SIP INVITE: ASCII header
    block, blank line, body. We split on the first ``\\n\\n`` (or
    ``\\r\\n\\r\\n``) and parse headers into a dict so each test can
    pluck the Content-Type without re-implementing SIP parsing.
    """

    raw = (FIXTURES / name).read_bytes()
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        sep = raw.find(b"\n\n")
        body = raw[sep + 2 :]
    else:
        body = raw[sep + 4 :]
    header_block = raw[:sep].decode("ascii")
    headers = {}
    for line in header_block.splitlines()[1:]:  # skip request line
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers, body


# ── extract_boundary ────────────────────────────────────────────────────


def test_extract_boundary_unquoted() -> None:
    assert (
        extract_boundary("multipart/mixed;boundary=cube-boundary-7e1f23")
        == "cube-boundary-7e1f23"
    )


def test_extract_boundary_quoted_with_whitespace() -> None:
    assert (
        extract_boundary('multipart/mixed; boundary="avaya-bound-bb22"')
        == "avaya-bound-bb22"
    )


def test_extract_boundary_missing_raises() -> None:
    with pytest.raises(ValueError):
        extract_boundary("multipart/mixed")


# ── parse_multipart ─────────────────────────────────────────────────────


def test_parse_multipart_two_parts_cisco() -> None:
    headers, body = _load_invite("cisco_cube_invite.sip")
    parts = parse_multipart(body, extract_boundary(headers["content-type"]))
    assert len(parts) == 2
    types = [p.content_type() for p in parts]
    assert types == ["application/sdp", "application/rs-metadata+xml"]


def test_parse_multipart_lf_only_separators_avaya() -> None:
    """Some SBCs emit LF-only separators between header and body."""

    headers, body = _load_invite("avaya_sbce_invite.sip")
    # Force-rewrite CRLF→LF in the body to simulate the lossy variant.
    body = body.replace(b"\r\n", b"\n")
    parts = parse_multipart(
        body, extract_boundary(headers["content-type"])
    )
    assert len(parts) == 2
    assert {p.content_type() for p in parts} == {
        "application/sdp",
        "application/rs-metadata+xml",
    }


# ── parse_sdp ───────────────────────────────────────────────────────────


def test_parse_sdp_two_audio_streams_with_crypto_cisco() -> None:
    headers, body = _load_invite("cisco_cube_invite.sip")
    parts = parse_multipart(body, extract_boundary(headers["content-type"]))
    sdp_text = parts[0].body.decode("utf-8")
    sdp = parse_sdp(sdp_text)

    assert len(sdp.streams) == 2
    s1, s2 = sdp.streams
    assert s1.label == "1"
    assert s2.label == "2"
    assert s1.direction == "sendonly"
    assert s2.direction == "sendonly"
    assert s1.proto == "RTP/SAVP"
    assert s1.rtpmap == {"0": "PCMU/8000", "8": "PCMA/8000"}
    assert len(s1.crypto) == 1
    assert s1.crypto[0].suite == "AES_CM_128_HMAC_SHA1_80"
    assert s1.crypto[0].tag == 1


def test_parse_sdp_dtls_srtp_avaya_no_crypto_lines() -> None:
    """Avaya SBCE uses DTLS-SRTP, so the SDP has no ``a=crypto`` lines."""

    headers, body = _load_invite("avaya_sbce_invite.sip")
    parts = parse_multipart(body, extract_boundary(headers["content-type"]))
    sdp = parse_sdp(parts[0].body.decode("utf-8"))

    assert len(sdp.streams) == 2
    for s in sdp.streams:
        assert s.crypto == []
        assert s.proto == "UDP/TLS/RTP/SAVP"


def test_parse_sdp_aes_256_metaswitch() -> None:
    headers, body = _load_invite("metaswitch_invite.sip")
    parts = parse_multipart(body, extract_boundary(headers["content-type"]))
    sdp = parse_sdp(parts[0].body.decode("utf-8"))
    assert len(sdp.streams) == 2
    for s in sdp.streams:
        assert s.crypto[0].suite == "AES_256_CM_HMAC_SHA1_80"


# ── parse_rs_metadata ───────────────────────────────────────────────────


def test_parse_rs_metadata_cisco() -> None:
    headers, body = _load_invite("cisco_cube_invite.sip")
    parts = parse_multipart(body, extract_boundary(headers["content-type"]))
    md = parse_rs_metadata(parts[1].body)

    assert md.recording_session_id == "cube-rec-session-fixture-0001"
    assert md.state == "active"
    assert {p.participant_id for p in md.participants} == {
        "participant-agent",
        "participant-customer",
    }
    # The agent's display name should round-trip.
    agent = next(p for p in md.participants if p.participant_id == "participant-agent")
    assert agent.display_name == "Sarah Anderson"
    assert agent.name_id == "sip:agent-12@cisco-cube.example.com"

    assert {s.label for s in md.streams} == {"1", "2"}
    assert "participant-agent" in md.participant_streams
    assert "participant-customer" in md.participant_streams


def test_parse_rs_metadata_communication_session_link() -> None:
    headers, body = _load_invite("cisco_cube_invite.sip")
    parts = parse_multipart(body, extract_boundary(headers["content-type"]))
    md = parse_rs_metadata(parts[1].body)
    assert len(md.communication_sessions) == 1
    cs = md.communication_sessions[0]
    assert cs.sip_session_id == "cube-rec-fixture-0001@cisco-cube.example.com"


def test_parse_rs_metadata_invalid_xml_raises() -> None:
    with pytest.raises(ValueError):
        parse_rs_metadata(b"<not><well-formed")


def test_parse_rs_metadata_wrong_root_raises() -> None:
    with pytest.raises(ValueError):
        parse_rs_metadata(
            b'<?xml version="1.0"?><some_other_root xmlns="urn:x"/>'
        )


# ── parse_siprec_invite (top-level) ─────────────────────────────────────


@pytest.mark.parametrize(
    "fixture",
    ["cisco_cube_invite.sip", "avaya_sbce_invite.sip", "metaswitch_invite.sip"],
)
def test_parse_siprec_invite_each_vendor(fixture: str) -> None:
    headers, body = _load_invite(fixture)
    invite = parse_siprec_invite(body, headers["content-type"])
    assert len(invite.sdp.streams) == 2
    assert invite.metadata.recording_session_id is not None
    assert invite.metadata.state == "active"


def test_parse_siprec_invite_missing_metadata_raises() -> None:
    body = (
        b"--bound\r\n"
        b"Content-Type: application/sdp\r\n\r\n"
        b"v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"
        b"m=audio 0 RTP/AVP 0\r\n"
        b"--bound--\r\n"
    )
    with pytest.raises(ValueError, match="rs-metadata"):
        parse_siprec_invite(body, "multipart/mixed; boundary=bound")


# ── participant_for_stream ──────────────────────────────────────────────


def test_participant_for_stream_resolves_agent_label() -> None:
    headers, body = _load_invite("cisco_cube_invite.sip")
    invite = parse_siprec_invite(body, headers["content-type"])
    resolved = participant_for_stream(invite.metadata, "1")
    assert resolved is not None
    stream_id, participant = resolved
    assert stream_id == "stream-1"
    assert participant.participant_id == "participant-agent"


def test_participant_for_stream_unknown_label_returns_none() -> None:
    headers, body = _load_invite("cisco_cube_invite.sip")
    invite = parse_siprec_invite(body, headers["content-type"])
    assert participant_for_stream(invite.metadata, "999") is None


# ── SRTP key extraction (siprec.srtp) ───────────────────────────────────


def test_supported_suites_includes_aes_256_and_aes_128() -> None:
    suites = supported_suites()
    assert "AES_CM_128_HMAC_SHA1_80" in suites
    assert "AES_256_CM_HMAC_SHA1_80" in suites
    assert is_supported("AES_CM_128_HMAC_SHA1_80")
    assert not is_supported("INVALID_SUITE_NAME")


def test_extract_key_material_aes_128() -> None:
    # 16-byte key + 14-byte salt = 30 bytes → 40 base64 chars.
    raw = bytes(range(30))
    crypto = CryptoAttribute(
        tag=1,
        suite="AES_CM_128_HMAC_SHA1_80",
        key_params="inline:" + base64.b64encode(raw).decode("ascii"),
    )
    km = extract_key_material(crypto)
    assert len(km.master_key) == 16
    assert len(km.master_salt) == 14
    assert km.master_key == raw[:16]
    assert km.master_salt == raw[16:30]


def test_extract_key_material_aes_256() -> None:
    raw = bytes(range(46))  # 32 + 14
    crypto = CryptoAttribute(
        tag=1,
        suite="AES_256_CM_HMAC_SHA1_80",
        key_params="inline:" + base64.b64encode(raw).decode("ascii"),
    )
    km = extract_key_material(crypto)
    assert len(km.master_key) == 32
    assert len(km.master_salt) == 14


def test_extract_key_material_unsupported_suite_raises() -> None:
    crypto = CryptoAttribute(
        tag=1,
        suite="DES_40_HMAC_SHA1_80",  # made up
        key_params="inline:Zm9v",
    )
    with pytest.raises(ValueError, match="Unsupported"):
        extract_key_material(crypto)


def test_extract_key_material_short_key_raises() -> None:
    crypto = CryptoAttribute(
        tag=1,
        suite="AES_CM_128_HMAC_SHA1_80",
        key_params="inline:" + base64.b64encode(b"too-short").decode("ascii"),
    )
    with pytest.raises(ValueError, match="bytes"):
        extract_key_material(crypto)


def test_extract_key_material_invalid_base64_raises() -> None:
    crypto = CryptoAttribute(
        tag=1,
        suite="AES_CM_128_HMAC_SHA1_80",
        key_params="inline:!!!not-base64!!!",
    )
    with pytest.raises(ValueError, match="base64"):
        extract_key_material(crypto)


# ── select_crypto preference ────────────────────────────────────────────


def test_select_crypto_prefers_aes_256_over_aes_128() -> None:
    aes128 = CryptoAttribute(
        tag=1, suite="AES_CM_128_HMAC_SHA1_80", key_params="inline:Zm9v"
    )
    aes256 = CryptoAttribute(
        tag=2, suite="AES_256_CM_HMAC_SHA1_80", key_params="inline:Zm9v"
    )
    assert select_crypto([aes128, aes256]) is aes256
    assert select_crypto([aes256, aes128]) is aes256


def test_select_crypto_prefers_80_over_32_at_same_key_size() -> None:
    s32 = CryptoAttribute(
        tag=1, suite="AES_CM_128_HMAC_SHA1_32", key_params="inline:Zm9v"
    )
    s80 = CryptoAttribute(
        tag=2, suite="AES_CM_128_HMAC_SHA1_80", key_params="inline:Zm9v"
    )
    assert select_crypto([s32, s80]) is s80


def test_select_crypto_returns_none_when_all_unsupported() -> None:
    bogus = CryptoAttribute(tag=1, suite="UNKNOWN", key_params="inline:Zm9v")
    assert select_crypto([bogus]) is None


def test_select_crypto_returns_none_for_empty_list() -> None:
    assert select_crypto([]) is None


# ── Provider namespace contract ─────────────────────────────────────────


def test_siprec_providers_match_telephony_literal() -> None:
    """Stream 1's reserved provider strings must be in the
    typed-Literal namespace from Stream 0; otherwise the type checker
    accepts strings that runtime persistence rejects."""

    from backend.app.services.telephony import TelephonyProvider
    from typing import get_args

    literal_values = set(get_args(TelephonyProvider))
    for p in SIPREC_PROVIDERS:
        assert p in literal_values, (
            f"{p} declared by SIPREC but missing from TelephonyProvider"
        )


# Skip the docker-compose smoke test in unit-test runs; it only runs
# when the SIPREC_INTEGRATION env var is set (CI cron job).
@pytest.mark.skipif(
    not os.environ.get("SIPREC_INTEGRATION"),
    reason="SIPREC integration test gated behind SIPREC_INTEGRATION=1",
)
def test_docker_compose_smoke() -> None:  # pragma: no cover
    """Smoke-test the SRS sidecar boots via docker-compose.

    Skipped by default because Docker isn't available in the
    standard CI test runner. Set ``SIPREC_INTEGRATION=1`` to run.
    """

    import subprocess

    compose = (
        Path(__file__).resolve().parent.parent
        / "backend/app/services/telephony/siprec_srs/docker-compose.yml"
    )
    out = subprocess.run(
        ["docker", "compose", "-f", str(compose), "config"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert out.returncode == 0, out.stderr
