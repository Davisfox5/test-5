"""Unit tests for ``services.telephony.audiohook.protocol``.

Pin the message-shape contract: parsing, format selection, binary
decoding, byte-order conversion. These are pure logic — no DB or
WebSocket needed.
"""

from __future__ import annotations

import json

import pytest

from backend.app.services.audio.normalizer import AudioFormat
from backend.app.services.telephony.audiohook.protocol import (
    CONNECTION_TYPE_AUDIO,
    CONNECTION_TYPE_PROBE,
    AudiohookMessageType,
    AudiohookOpenMessage,
    AudiohookOpenedMessage,
    AudiohookProtocolError,
    MediaFormat,
    decode_audio_frame,
    encode_control_message,
    parse_control_message,
    select_media_format,
)


# ── parse_control_message ───────────────────────────────────────────────


def test_parse_control_message_accepts_well_formed_json():
    raw = json.dumps(
        {
            "version": "2",
            "id": "abc",
            "type": "open",
            "seq": 1,
            "parameters": {"organizationId": "org"},
        }
    )
    msg = parse_control_message(raw)
    assert msg["type"] == "open"
    assert msg["seq"] == 1
    assert msg["parameters"]["organizationId"] == "org"


def test_parse_control_message_rejects_non_json():
    with pytest.raises(AudiohookProtocolError):
        parse_control_message("not json {")


def test_parse_control_message_rejects_non_object():
    with pytest.raises(AudiohookProtocolError):
        parse_control_message("[1, 2, 3]")


def test_parse_control_message_requires_string_type():
    raw = json.dumps({"id": "x", "type": 42, "seq": 1})
    with pytest.raises(AudiohookProtocolError):
        parse_control_message(raw)


def test_parse_control_message_requires_int_seq():
    raw = json.dumps({"id": "x", "type": "ping", "seq": "1"})
    with pytest.raises(AudiohookProtocolError):
        parse_control_message(raw)


def test_parse_control_message_accepts_bytes_input():
    raw = json.dumps({"id": "x", "type": "ping", "seq": 5}).encode("utf-8")
    msg = parse_control_message(raw)
    assert msg["seq"] == 5


# ── encode_control_message ──────────────────────────────────────────────


def test_encode_control_message_emits_required_envelope_fields():
    out = encode_control_message(
        version="2",
        msg_type=AudiohookMessageType.OPENED,
        seq=7,
        client_seq=3,
        session_id="sess-1",
        parameters={"startPaused": False},
    )
    decoded = json.loads(out)
    assert decoded == {
        "version": "2",
        "id": "sess-1",
        "type": "opened",
        "seq": 7,
        "clientseq": 3,
        "parameters": {"startPaused": False},
    }


def test_encode_control_message_accepts_string_type():
    out = encode_control_message(
        version="2",
        msg_type="custom",
        seq=1,
        client_seq=0,
        session_id="x",
    )
    assert json.loads(out)["type"] == "custom"


# ── MediaFormat / select_media_format ──────────────────────────────────


def test_media_format_to_audio_format_pcmu():
    fmt = MediaFormat(type="audio", format="PCMU", rate=8000, channels=("external",))
    assert fmt.to_audio_format() == AudioFormat.MULAW_8K


def test_media_format_to_audio_format_l16_8k_and_16k():
    assert (
        MediaFormat(type="audio", format="L16", rate=8000).to_audio_format()
        == AudioFormat.PCM16_8K
    )
    assert (
        MediaFormat(type="audio", format="L16", rate=16000).to_audio_format()
        == AudioFormat.PCM16_16K
    )


def test_media_format_to_audio_format_rejects_unknown():
    with pytest.raises(ValueError):
        MediaFormat(type="audio", format="OPUS", rate=48000).to_audio_format()


def test_select_media_format_prefers_pcmu_when_offered():
    offered = [
        MediaFormat(type="audio", format="L16", rate=16000),
        MediaFormat(type="audio", format="PCMU", rate=8000),
    ]
    chosen = select_media_format(offered)
    assert chosen is not None
    assert chosen.format == "PCMU"


def test_select_media_format_falls_back_to_l16():
    offered = [MediaFormat(type="audio", format="L16", rate=16000)]
    chosen = select_media_format(offered)
    assert chosen is not None
    assert chosen.format == "L16"
    assert chosen.rate == 16000


def test_select_media_format_returns_none_for_unsupported_offer():
    offered = [MediaFormat(type="audio", format="OPUS", rate=48000)]
    assert select_media_format(offered) is None


def test_select_media_format_returns_none_for_empty_offer():
    assert select_media_format([]) is None


# ── AudiohookOpenMessage ────────────────────────────────────────────────


def test_open_message_parses_audio_connection():
    raw = {
        "organizationId": "org-1",
        "conversationId": "conv-1",
        "participant": {"id": "part-1"},
        "type": "audio",
        "media": [
            {
                "type": "audio",
                "format": "PCMU",
                "rate": 8000,
                "channels": ["external", "internal"],
            }
        ],
        "language": "en-US",
    }
    msg = AudiohookOpenMessage.from_parameters(raw)
    assert msg.organization_id == "org-1"
    assert msg.conversation_id == "conv-1"
    assert msg.participant_id == "part-1"
    assert msg.connection_type == CONNECTION_TYPE_AUDIO
    assert len(msg.media) == 1
    assert msg.media[0].format == "PCMU"
    assert msg.media[0].channels == ("external", "internal")
    assert msg.language == "en-US"
    assert msg.raw == raw


def test_open_message_parses_probe_connection_with_no_media():
    raw = {
        "organizationId": "org-2",
        "conversationId": "conv-2",
        "participant": {"id": "part-2"},
        "type": "connectionProbe",
        "media": [],
    }
    msg = AudiohookOpenMessage.from_parameters(raw)
    assert msg.connection_type == CONNECTION_TYPE_PROBE
    assert msg.media == []


# ── AudiohookOpenedMessage ──────────────────────────────────────────────


def test_opened_message_with_media_serializes_one_entry():
    chosen = MediaFormat(
        type="audio", format="PCMU", rate=8000, channels=("external",)
    )
    params = AudiohookOpenedMessage(media=chosen, start_paused=False).to_parameters()
    assert params["startPaused"] is False
    assert len(params["media"]) == 1
    assert params["media"][0] == {
        "type": "audio",
        "format": "PCMU",
        "rate": 8000,
        "channels": ["external"],
    }


def test_opened_message_with_no_media_serializes_empty_list():
    params = AudiohookOpenedMessage(media=None).to_parameters()
    assert params["media"] == []


# ── decode_audio_frame ──────────────────────────────────────────────────


def test_decode_audio_frame_pcmu_passthrough():
    fmt = MediaFormat(type="audio", format="PCMU", rate=8000, channels=("external",))
    payload = bytes([0x7F, 0x80, 0xFF, 0x00])
    assert decode_audio_frame(payload, fmt) == payload


def test_decode_audio_frame_l16_byte_swaps_to_little_endian():
    fmt = MediaFormat(type="audio", format="L16", rate=16000, channels=("external",))
    # Big-endian samples 0x0102, 0x0304 → little-endian 0x02,0x01,0x04,0x03.
    big_endian = bytes([0x01, 0x02, 0x03, 0x04])
    assert decode_audio_frame(big_endian, fmt) == bytes([0x02, 0x01, 0x04, 0x03])


def test_decode_audio_frame_l16_rejects_odd_length():
    fmt = MediaFormat(type="audio", format="L16", rate=8000)
    with pytest.raises(AudiohookProtocolError):
        decode_audio_frame(bytes([0x01, 0x02, 0x03]), fmt)


def test_decode_audio_frame_empty_payload_returns_empty():
    fmt = MediaFormat(type="audio", format="L16", rate=16000)
    assert decode_audio_frame(b"", fmt) == b""


def test_decode_audio_frame_unknown_format_raises():
    fmt = MediaFormat(type="audio", format="OPUS", rate=48000)
    with pytest.raises(AudiohookProtocolError):
        decode_audio_frame(b"\x00\x01", fmt)
