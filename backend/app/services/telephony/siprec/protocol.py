"""SIPREC wire-format parsers (RFC 7866 + RFC 7865).

A SIPREC INVITE from a Session Recording Client (SRC — the customer's
SBC) carries a ``multipart/mixed`` body with two parts:

1. ``application/sdp`` — the standard SDP offer, describing the RTP
   media streams the SRC will send. Each ``m=audio`` line maps to one
   participant's audio (caller or callee). SRTP key material lives in
   ``a=crypto`` lines (SDES) when DTLS-SRTP is not used.

2. ``application/rs-metadata+xml`` — the recording-session metadata
   defined in RFC 7865. Carries the SRC's call id, the participants'
   identities, the association between participants and SDP media
   streams, and (optionally) communication-session relationships.

This module parses both. It is pure-CPU, no I/O, and no third-party
dependencies — the SRS sidecar terminates the SIP layer; we only need
to interpret the bodies it forwards to us. Keeping it dependency-free
lets the protocol unit tests run in CI without spinning up FreeSWITCH.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ── Multipart MIME splitter ─────────────────────────────────────────────


_RE_BOUNDARY_PARAM = re.compile(
    r'boundary\s*=\s*("([^"]+)"|([^\s;]+))', re.IGNORECASE
)


def extract_boundary(content_type: str) -> str:
    """Extract the multipart boundary from a Content-Type header.

    Handles quoted (``boundary="abc"``) and unquoted forms. Raises
    ``ValueError`` if no boundary is present — callers should treat
    that as a malformed INVITE rather than an empty result.
    """

    m = _RE_BOUNDARY_PARAM.search(content_type or "")
    if not m:
        raise ValueError(
            f"Content-Type {content_type!r} has no multipart boundary parameter"
        )
    return m.group(2) or m.group(3)


@dataclass
class MimePart:
    """One part of a multipart body — headers (lower-cased keys) and
    the raw payload bytes."""

    headers: Dict[str, str]
    body: bytes

    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def parse_multipart(body: bytes, boundary: str) -> List[MimePart]:
    """Split a multipart body into its constituent parts.

    Implements just enough of RFC 2046 for SIPREC INVITEs (no nested
    multiparts, no transfer encodings — those are out of scope for the
    profile). The returned list preserves order so callers can rely on
    the SDP arriving before the metadata XML when an SRC sends them
    that way.
    """

    if not boundary:
        raise ValueError("Empty multipart boundary")

    delim = b"--" + boundary.encode("ascii")
    end = delim + b"--"

    # Strip preamble (anything before the first boundary) and epilogue
    # (anything after the closing boundary).
    first = body.find(delim)
    if first < 0:
        raise ValueError("No opening boundary in multipart body")
    body = body[first:]

    parts: List[MimePart] = []
    chunks = body.split(delim)
    # ``chunks[0]`` is the empty preamble that preceded the first
    # boundary; skip it. The last chunk is either the closing
    # ``--<boundary>--`` marker or a trailing empty string.
    for chunk in chunks[1:]:
        # End-of-multipart marker: ``--`` immediately after the
        # boundary. Anything past it is the epilogue.
        if chunk.startswith(b"--"):
            break
        # Each chunk begins with a CRLF (or LF) that follows the
        # boundary line; trim it before header parsing.
        chunk = chunk.lstrip(b"\r\n")
        # Headers and body are separated by a blank line.
        sep_idx = _find_header_body_sep(chunk)
        if sep_idx < 0:
            # No header/body separator — treat the entire chunk as
            # body with no headers (fault-tolerant; lets us still
            # surface SRC bugs in higher-level validation).
            parts.append(MimePart(headers={}, body=chunk.rstrip(b"\r\n")))
            continue
        header_blob = chunk[:sep_idx].decode("ascii", errors="replace")
        # Body trails the separator and a trailing CRLF before the
        # next boundary; strip just that one trailing CRLF.
        part_body = chunk[sep_idx:]
        # Drop the leading separator (CRLFCRLF or LFLF).
        if part_body.startswith(b"\r\n\r\n"):
            part_body = part_body[4:]
        elif part_body.startswith(b"\n\n"):
            part_body = part_body[2:]
        # Strip the single CRLF that precedes the next boundary line.
        if part_body.endswith(b"\r\n"):
            part_body = part_body[:-2]
        elif part_body.endswith(b"\n"):
            part_body = part_body[:-1]
        headers = _parse_headers(header_blob)
        parts.append(MimePart(headers=headers, body=part_body))
    return parts


def _find_header_body_sep(chunk: bytes) -> int:
    """Return the index of the first byte of the header/body separator,
    or -1 if absent. Accepts both ``\\r\\n\\r\\n`` (RFC) and ``\\n\\n``
    (some SBCs emit LF-only) forms."""

    crlf = chunk.find(b"\r\n\r\n")
    lf = chunk.find(b"\n\n")
    if crlf < 0:
        return lf
    if lf < 0:
        return crlf
    return min(crlf, lf)


def _parse_headers(blob: str) -> Dict[str, str]:
    """Parse a folded RFC-822 header block into a dict (keys lowered)."""

    # Unfold continuation lines (start with whitespace).
    unfolded: List[str] = []
    for line in blob.splitlines():
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += " " + line.strip()
        else:
            unfolded.append(line)
    out: Dict[str, str] = {}
    for line in unfolded:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip().lower()] = v.strip()
    return out


# ── SDP parser (just enough of RFC 4566 for SIPREC) ─────────────────────


@dataclass
class CryptoAttribute:
    """One ``a=crypto`` line (RFC 4568) on an audio media section."""

    tag: int
    suite: str  # e.g. AES_CM_128_HMAC_SHA1_80
    key_params: str  # full key-params string (caller decodes via siprec.srtp)


@dataclass
class MediaStream:
    """One ``m=audio ...`` block from an SDP offer."""

    media: str  # always "audio" for SIPREC ingest
    port: int
    proto: str  # RTP/AVP, RTP/SAVP, UDP/TLS/RTP/SAVP, ...
    formats: List[str]  # payload types
    label: Optional[str]  # ``a=label:`` (ties stream → rs-metadata <stream>)
    direction: Optional[str]  # sendonly/recvonly/sendrecv/inactive
    rtpmap: Dict[str, str]  # payload type → "PCMU/8000" etc.
    crypto: List[CryptoAttribute]
    connection: Optional[str]  # ``c=IN IP4 ...`` if present at media level


@dataclass
class SdpDescription:
    session_id: Optional[str]
    session_origin: Optional[str]
    session_connection: Optional[str]
    streams: List[MediaStream]


def parse_sdp(sdp: str) -> SdpDescription:
    """Parse an SDP offer into its session-level fields and audio streams.

    Only the lines SIPREC actually needs are interpreted. Unknown
    attributes are tolerated — SBCs love adding vendor-specific
    attributes that we should pass through, not fail on.
    """

    session_id: Optional[str] = None
    session_origin: Optional[str] = None
    session_connection: Optional[str] = None
    streams: List[MediaStream] = []
    current: Optional[MediaStream] = None
    in_session = True

    for raw in sdp.replace("\r\n", "\n").split("\n"):
        if not raw or "=" not in raw:
            continue
        kind, value = raw[0], raw[2:]
        if kind == "o":  # origin: username sess-id sess-version nettype addrtype unicast
            session_origin = value
            parts = value.split()
            if len(parts) >= 2:
                session_id = parts[1]
        elif kind == "c" and in_session:
            session_connection = value
        elif kind == "m":
            in_session = False
            mparts = value.split()
            if len(mparts) < 4 or mparts[0] != "audio":
                # Non-audio media — represent as a placeholder so
                # ``a=`` lines that follow don't accidentally get
                # attached to a previous audio stream.
                current = None
                continue
            try:
                port = int(mparts[1])
            except ValueError:
                port = 0
            current = MediaStream(
                media=mparts[0],
                port=port,
                proto=mparts[2],
                formats=mparts[3:],
                label=None,
                direction=None,
                rtpmap={},
                crypto=[],
                connection=None,
            )
            streams.append(current)
        elif kind == "c" and current is not None:
            current.connection = value
        elif kind == "a" and current is not None:
            _apply_media_attribute(current, value)
        # ``a=`` at session level is ignored — none of the
        # session-level attrs SIPREC defines (e.g. ``a=group``) are
        # load-bearing for our ingest path.

    return SdpDescription(
        session_id=session_id,
        session_origin=session_origin,
        session_connection=session_connection,
        streams=streams,
    )


def _apply_media_attribute(stream: MediaStream, value: str) -> None:
    """Apply one ``a=`` line to the current audio media stream."""

    if value in ("sendonly", "recvonly", "sendrecv", "inactive"):
        stream.direction = value
        return
    name, _, rest = value.partition(":")
    name = name.strip().lower()
    rest = rest.strip()
    if name == "label":
        stream.label = rest
    elif name == "rtpmap":
        # ``a=rtpmap:0 PCMU/8000``
        pt, _, encoding = rest.partition(" ")
        if pt:
            stream.rtpmap[pt.strip()] = encoding.strip()
    elif name == "crypto":
        crypto = _parse_crypto(rest)
        if crypto is not None:
            stream.crypto.append(crypto)


_RE_CRYPTO = re.compile(
    r"^\s*(?P<tag>\d+)\s+(?P<suite>\S+)\s+(?P<keyparams>\S.*?)\s*(?:;.*)?$"
)


def _parse_crypto(rest: str) -> Optional[CryptoAttribute]:
    """Parse the body of an ``a=crypto:`` line.

    The full attribute is ``a=crypto:<tag> <suite> <key-params> [session-params]``.
    We strip session-params (anything after the first whitespace block
    following key-params); the SDES decoder in ``siprec.srtp`` only
    needs the suite + key-params.
    """

    m = _RE_CRYPTO.match(rest)
    if not m:
        return None
    try:
        tag = int(m.group("tag"))
    except ValueError:
        return None
    return CryptoAttribute(
        tag=tag,
        suite=m.group("suite"),
        # Drop any trailing session-params separated by whitespace.
        key_params=m.group("keyparams").split()[0],
    )


# ── rs-metadata XML parser (RFC 7865) ───────────────────────────────────


# RFC 7865 namespace URI — ElementTree doesn't strip namespaces, so we
# match against the Clark-notation prefix below.
_NS = "urn:ietf:params:xml:ns:recording:1"


@dataclass
class RsParticipant:
    """One ``<participant>`` element in rs-metadata."""

    participant_id: str
    name_id: Optional[str] = None  # the ``<nameID aor="...">`` AOR
    display_name: Optional[str] = None
    associate_time: Optional[str] = None
    disassociate_time: Optional[str] = None


@dataclass
class RsStream:
    """One ``<stream>`` element — ties an SDP media label to a participant."""

    stream_id: str
    session_id: str  # the recording session this stream belongs to
    label: Optional[str] = None  # SDP ``a=label`` value
    associate_time: Optional[str] = None
    disassociate_time: Optional[str] = None


@dataclass
class RsCommunicationSession:
    """A communication-session reference — links the recorded call to
    the original SIP dialog (the ``<sipSessionID>`` element)."""

    session_id: str
    sip_session_id: Optional[str] = None
    group_ref: Optional[str] = None


@dataclass
class RsMetadata:
    """Top-level rs-metadata document — what we hand to higher layers."""

    recording_session_id: Optional[str]
    state: Optional[str]  # complete | partial | terminated
    participants: List[RsParticipant] = field(default_factory=list)
    streams: List[RsStream] = field(default_factory=list)
    communication_sessions: List[RsCommunicationSession] = field(
        default_factory=list
    )
    # Map: participant_id -> [stream_id, ...] from
    # ``<participantstreamassoc>``. Lets the bridge route audio frames
    # to the right participant ("agent" vs "customer") downstream.
    participant_streams: Dict[str, List[str]] = field(default_factory=dict)


def parse_rs_metadata(xml: bytes | str) -> RsMetadata:
    """Parse an rs-metadata XML body.

    Tolerates both ``application/rs-metadata+xml`` (the IANA-registered
    type) and the misspelled ``application/recording+xml`` some legacy
    SBCs still emit — the type check happens at the multipart layer,
    so this function only sees the bytes.

    Raises ``ValueError`` for documents that aren't well-formed XML or
    that lack a ``<recording>`` root in the RFC 7865 namespace.
    """

    if isinstance(xml, bytes):
        try:
            xml = xml.decode("utf-8")
        except UnicodeDecodeError:
            xml = xml.decode("latin-1")

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"rs-metadata XML is not well-formed: {exc}") from exc

    # Accept either the namespaced root tag or the bare ``recording``
    # form (some SBCs emit no namespace declaration).
    tag = _local(root.tag)
    if tag != "recording":
        raise ValueError(
            f"rs-metadata root element is <{root.tag}>, expected <recording>"
        )

    md = RsMetadata(
        recording_session_id=root.get("session_id") or root.get("id"),
        state=root.get("state"),
    )

    for child in root:
        ctag = _local(child.tag)
        if ctag == "session":
            md.recording_session_id = (
                child.get("session_id")
                or child.get("id")
                or md.recording_session_id
            )
            md.state = child.get("state") or md.state
        elif ctag == "participant":
            md.participants.append(_parse_participant(child))
        elif ctag == "stream":
            md.streams.append(_parse_stream(child))
        elif ctag == "communicationsession":
            md.communication_sessions.append(_parse_comm_session(child))
        elif ctag == "participantstreamassoc":
            pid = child.get("participant_id")
            if not pid:
                continue
            stream_ids: List[str] = []
            for s in child:
                if _local(s.tag) == "send" or _local(s.tag) == "recv":
                    sid = (s.text or "").strip()
                    if sid:
                        stream_ids.append(sid)
            if stream_ids:
                md.participant_streams.setdefault(pid, []).extend(stream_ids)

    return md


def _local(tag: str) -> str:
    """Strip the XML namespace from a Clark-notation tag.

    ``{urn:ietf:params:xml:ns:recording:1}participant`` → ``participant``.
    Tag-name comparison stays case-insensitive because at least one
    SBC vendor emits camelCase (``<participantStreamAssoc>``) while
    the RFC uses lowercase.
    """

    if tag.startswith("{"):
        tag = tag.split("}", 1)[1]
    return tag.lower()


def _parse_participant(elem: ET.Element) -> RsParticipant:
    pid = elem.get("participant_id") or elem.get("id") or ""
    name_id: Optional[str] = None
    display_name: Optional[str] = None
    associate_time: Optional[str] = None
    disassociate_time: Optional[str] = None
    for c in elem:
        ctag = _local(c.tag)
        if ctag == "nameid" or ctag == "name_id":
            name_id = c.get("aor") or (c.text or "").strip() or None
            # Display name may be in the element text when ``aor`` is
            # an attribute, or in a child ``<name>`` element.
            for nc in c:
                if _local(nc.tag) == "name":
                    display_name = (nc.text or "").strip() or display_name
        elif ctag == "name":
            display_name = (c.text or "").strip() or display_name
        elif ctag == "associate-time" or ctag == "associatetime":
            associate_time = (c.text or "").strip() or None
        elif ctag == "disassociate-time" or ctag == "disassociatetime":
            disassociate_time = (c.text or "").strip() or None
    return RsParticipant(
        participant_id=pid,
        name_id=name_id,
        display_name=display_name,
        associate_time=associate_time,
        disassociate_time=disassociate_time,
    )


def _parse_stream(elem: ET.Element) -> RsStream:
    stream_id = elem.get("stream_id") or elem.get("id") or ""
    session_id = elem.get("session_id") or ""
    label: Optional[str] = None
    associate_time: Optional[str] = None
    disassociate_time: Optional[str] = None
    for c in elem:
        ctag = _local(c.tag)
        if ctag == "label":
            label = (c.text or "").strip() or None
        elif ctag == "associate-time" or ctag == "associatetime":
            associate_time = (c.text or "").strip() or None
        elif ctag == "disassociate-time" or ctag == "disassociatetime":
            disassociate_time = (c.text or "").strip() or None
    return RsStream(
        stream_id=stream_id,
        session_id=session_id,
        label=label,
        associate_time=associate_time,
        disassociate_time=disassociate_time,
    )


def _parse_comm_session(elem: ET.Element) -> RsCommunicationSession:
    session_id = elem.get("session_id") or elem.get("id") or ""
    sip_session_id: Optional[str] = None
    group_ref: Optional[str] = None
    for c in elem:
        ctag = _local(c.tag)
        if ctag == "sipsessionid":
            sip_session_id = (c.text or "").strip() or None
        elif ctag == "group" or ctag == "groupref":
            group_ref = c.get("group_ref") or (c.text or "").strip() or None
    return RsCommunicationSession(
        session_id=session_id,
        sip_session_id=sip_session_id,
        group_ref=group_ref,
    )


# ── Top-level: parse a SIPREC INVITE body ───────────────────────────────


@dataclass
class SiprecInvite:
    """The pieces of a SIPREC INVITE we care about, fully parsed."""

    sdp: SdpDescription
    metadata: RsMetadata
    extra_parts: List[MimePart] = field(default_factory=list)


def parse_siprec_invite(body: bytes, content_type: str) -> SiprecInvite:
    """Top-level entry — split the multipart body, parse SDP + metadata.

    Raises ``ValueError`` when neither part is found; callers should
    treat that as a malformed INVITE (415 Unsupported Media Type at
    the SRS, but at the bridge layer it indicates an SRS bug because
    the SRS already enforced the media type).
    """

    boundary = extract_boundary(content_type)
    parts = parse_multipart(body, boundary)

    sdp_part: Optional[MimePart] = None
    md_part: Optional[MimePart] = None
    extras: List[MimePart] = []
    for p in parts:
        ct = p.content_type()
        if ct == "application/sdp" and sdp_part is None:
            sdp_part = p
        elif ct in (
            "application/rs-metadata+xml",
            "application/rs-metadata",
            "application/recording+xml",
        ) and md_part is None:
            md_part = p
        else:
            extras.append(p)

    if sdp_part is None:
        raise ValueError("SIPREC INVITE missing application/sdp part")
    if md_part is None:
        raise ValueError(
            "SIPREC INVITE missing application/rs-metadata+xml part"
        )

    sdp = parse_sdp(sdp_part.body.decode("utf-8", errors="replace"))
    metadata = parse_rs_metadata(md_part.body)
    return SiprecInvite(sdp=sdp, metadata=metadata, extra_parts=extras)


def participant_for_stream(
    metadata: RsMetadata, stream_label: str
) -> Optional[Tuple[str, RsParticipant]]:
    """Resolve an SDP ``a=label`` value to its rs-metadata participant.

    Returns ``(stream_id, participant)`` or ``None`` if the metadata
    doesn't contain a participantstreamassoc that includes a stream
    matching the label. Used by the bridge to tag inbound audio frames
    with the right speaker so downstream diarization keeps "agent" and
    "customer" channels separate.
    """

    label_to_stream: Dict[str, str] = {}
    for s in metadata.streams:
        if s.label:
            label_to_stream[s.label] = s.stream_id

    target_stream = label_to_stream.get(stream_label)
    if target_stream is None:
        return None

    for pid, stream_ids in metadata.participant_streams.items():
        if target_stream in stream_ids:
            for p in metadata.participants:
                if p.participant_id == pid:
                    return target_stream, p
    return None
