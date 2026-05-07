"""Genesys Cloud AudioHook integration (Stream 4 of multi-stream telephony work).

The AudioHook Protocol is a Genesys-published WebSocket protocol that
streams real-time audio from Genesys Cloud's AudioHook Monitor into a
third-party server (this package). Spec source of truth:
https://developer.genesys.cloud/devapps/audiohook/

Submodules:

* :mod:`.protocol` — message-type constants, JSON validation, binary
  frame decoding into :class:`backend.app.services.audio.AudioFormat`.
* :mod:`.auth` — HMAC-SHA256 verification of the signed upgrade
  request. Genesys signs with the integration's client secret stored
  on ``Integration.provider_config``.
* :mod:`.server` — per-connection session state machine: probe ack,
  open negotiation, ping/pong, pause/resume, audio dispatch, close.

The package owns the ``genesys_audiohook`` value in
:data:`backend.app.services.telephony.TelephonyProvider`. No other
stream may write that value into ``Integration.provider``.
"""

from backend.app.services.telephony.audiohook.protocol import (
    AudiohookMessageType,
    AudiohookOpenMessage,
    AudiohookOpenedMessage,
    MediaFormat,
    decode_audio_frame,
    encode_control_message,
    parse_control_message,
)

__all__ = [
    "AudiohookMessageType",
    "AudiohookOpenMessage",
    "AudiohookOpenedMessage",
    "MediaFormat",
    "decode_audio_frame",
    "encode_control_message",
    "parse_control_message",
]
