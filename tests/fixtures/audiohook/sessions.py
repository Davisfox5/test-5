"""Hand-built AudioHook session fixtures.

These mirror the shape of frames Genesys Cloud emits during a real
session, distilled from the AudioHook spec at
https://developer.genesys.cloud/devapps/audiohook/. Hand-built rather
than recorded because we don't connect to a real Genesys org during
Stream 4 (per the plan's "synthetic-fixture acceptance only" rule).

Each fixture is a list of inbound text/binary frames. The test
harness asserts the server's outbound responses match a parallel
expected list — see ``tests/test_audiohook_server.py``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List


SESSION_ID = "00000000-0000-0000-0000-000000000abc"
ORG_ID = "11111111-1111-1111-1111-111111111111"
CONVERSATION_ID = "22222222-2222-2222-2222-222222222222"
PARTICIPANT_ID = "33333333-3333-3333-3333-333333333333"


def _ctl(msg_type: str, seq: int, parameters: Dict[str, Any]) -> str:
    """Build one client-side control frame as a JSON string."""

    return json.dumps(
        {
            "version": "2",
            "id": SESSION_ID,
            "type": msg_type,
            "seq": seq,
            "serverseq": 0,
            "position": "PT0S",
            "parameters": parameters,
        }
    )


def probe_session() -> List[Dict[str, Any]]:
    """Connection-probe handshake: open(probe) → close.

    Genesys runs this when an admin saves an integration: it
    validates the WebSocket URL, signature, and that the server
    responds correctly. No audio flows. Server should reply
    ``opened`` (empty media) then ``closed`` and exit.
    """

    return [
        {
            "text": _ctl(
                "open",
                seq=1,
                parameters={
                    "organizationId": ORG_ID,
                    "conversationId": CONVERSATION_ID,
                    "participant": {"id": PARTICIPANT_ID},
                    "type": "connectionProbe",
                    "media": [],
                    "language": "en-US",
                },
            )
        },
        {"text": _ctl("close", seq=2, parameters={"reason": "end"})},
    ]


def audio_session_pcmu_with_pause() -> List[Dict[str, Any]]:
    """Full audio session: open → audio → pause/resume → close.

    Negotiates PCMU 8 kHz with both channels (agent + customer).
    Two binary audio frames bracket a paused/resumed cycle to
    exercise the PCI-pause path. The pause arrives mid-stream and
    is followed by a 16-byte audio frame that the server should
    DROP (because we're paused).
    """

    audio_pre = bytes([0x7F, 0x80, 0xFF, 0x00] * 16)  # 64-byte PCMU sample
    audio_paused = bytes([0xAA] * 16)  # would-be-leaked frame during pause
    audio_post = bytes([0x55, 0xAA] * 32)  # 64-byte after resume

    return [
        {
            "text": _ctl(
                "open",
                seq=1,
                parameters={
                    "organizationId": ORG_ID,
                    "conversationId": CONVERSATION_ID,
                    "participant": {"id": PARTICIPANT_ID},
                    "type": "audio",
                    "media": [
                        {
                            "type": "audio",
                            "format": "PCMU",
                            "rate": 8000,
                            "channels": ["external", "internal"],
                        },
                        {
                            "type": "audio",
                            "format": "L16",
                            "rate": 16000,
                            "channels": ["external", "internal"],
                        },
                    ],
                    "language": "en-US",
                },
            )
        },
        {"bytes": audio_pre},
        {"text": _ctl("ping", seq=2, parameters={"rtt": "PT0.05S"})},
        {"text": _ctl("paused", seq=3, parameters={})},
        # This frame must NOT reach the sink — we're paused.
        {"bytes": audio_paused},
        {"text": _ctl("resumed", seq=4, parameters={})},
        {"bytes": audio_post},
        {"text": _ctl("close", seq=5, parameters={"reason": "end"})},
    ]


def audio_session_l16_customer_only() -> List[Dict[str, Any]]:
    """Customer-leg-only L16 16 kHz audio session.

    Used to verify the server picks an offered L16 candidate when
    PCMU isn't on offer, byte-swaps the L16 payload (big-endian on
    the wire → little-endian for the normalizer), and labels the
    channel as ``"customer"``.
    """

    # Two big-endian PCM16 samples: 0x0102 and 0x0304. After byte
    # swap we expect 0x02, 0x01, 0x04, 0x03.
    audio = bytes([0x01, 0x02, 0x03, 0x04])

    return [
        {
            "text": _ctl(
                "open",
                seq=1,
                parameters={
                    "organizationId": ORG_ID,
                    "conversationId": CONVERSATION_ID,
                    "participant": {"id": PARTICIPANT_ID},
                    "type": "audio",
                    "media": [
                        {
                            "type": "audio",
                            "format": "L16",
                            "rate": 16000,
                            "channels": ["external"],
                        }
                    ],
                    "language": "en-US",
                },
            )
        },
        {"bytes": audio},
        {"text": _ctl("close", seq=2, parameters={"reason": "end"})},
    ]


def malformed_first_message() -> List[Dict[str, Any]]:
    """Negative case: client's first frame is ``ping`` instead of ``open``.

    Server must respond with ``error`` (code ``invalid-message``)
    and close. Used to lock the "open must be first" invariant.
    """

    return [
        {"text": _ctl("ping", seq=1, parameters={})},
    ]
