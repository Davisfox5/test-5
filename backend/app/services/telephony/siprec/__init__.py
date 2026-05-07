"""SIPREC ingestion (RFC 7866) — Stream 1 of the multi-stream telephony build.

The customer's SBC (Cisco CUBE / Avaya SBCE / Metaswitch Perimeta) acts as
the Session Recording Client (SRC) and forks each call's media to LINDA's
Session Recording Server. The SRS (FreeSWITCH userspace, see
``services/telephony/siprec_srs``) terminates SIP and SRTP, and forwards
control events + plaintext audio frames into ``SiprecBridge``.

Key entry points:

* ``parse_siprec_invite`` — parse the multipart-MIME body of a SIPREC
  INVITE into its SDP + rs-metadata pieces.
* ``select_crypto`` / ``extract_key_material`` — SDES validation; DTLS-SRTP
  is handled inside the SRS, not here.
* ``SiprecBridge`` — async lifecycle manager that turns SRS events into
  ``SiprecSession`` rows + transcription dispatches.

Per the multi-stream coordination plan, this package owns the SIPREC
provider strings ``siprec_cisco_cube``, ``siprec_avaya_sbce``, and
``siprec_metaswitch`` in ``Integration.provider``. See
``backend/app/services/telephony/__init__.py`` for the typed Literal.
"""

from backend.app.services.telephony.siprec.bridge import (
    SiprecAudioFrame,
    SiprecBridge,
    TranscriptionDispatch,
    get_bridge,
    set_bridge,
)
from backend.app.services.telephony.siprec.protocol import (
    CryptoAttribute,
    MediaStream,
    MimePart,
    RsMetadata,
    RsParticipant,
    RsStream,
    SdpDescription,
    SiprecInvite,
    extract_boundary,
    parse_multipart,
    parse_rs_metadata,
    parse_sdp,
    parse_siprec_invite,
    participant_for_stream,
)
from backend.app.services.telephony.siprec.srtp import (
    SrtpKeyMaterial,
    SuiteSpec,
    extract_key_material,
    is_supported,
    select_crypto,
    supported_suites,
)

# The set of TelephonyProvider Literal values this package owns. Used by
# the API layer to validate ``POST /admin/integrations/siprec`` requests.
SIPREC_PROVIDERS = (
    "siprec_cisco_cube",
    "siprec_avaya_sbce",
    "siprec_metaswitch",
)

__all__ = [
    "SIPREC_PROVIDERS",
    "CryptoAttribute",
    "MediaStream",
    "MimePart",
    "RsMetadata",
    "RsParticipant",
    "RsStream",
    "SdpDescription",
    "SiprecAudioFrame",
    "SiprecBridge",
    "SiprecInvite",
    "SrtpKeyMaterial",
    "SuiteSpec",
    "TranscriptionDispatch",
    "extract_boundary",
    "extract_key_material",
    "get_bridge",
    "is_supported",
    "parse_multipart",
    "parse_rs_metadata",
    "parse_sdp",
    "parse_siprec_invite",
    "participant_for_stream",
    "select_crypto",
    "set_bridge",
    "supported_suites",
]
