# Cisco CUBE — SIPREC SRC configuration

LINDA's Session Recording Server speaks SIPREC (RFC 7866). On the
Cisco side, **CUBE** (Cisco Unified Border Element) is the Session
Recording Client (SRC); it forks media from voice calls into LINDA's
SRS and provides RFC 7865 metadata as a multipart MIME body in the
INVITE.

This guide covers Cisco IOS-XE 17.x CUBE (the current LTS, 17.13.1a
verified). 16.x predates the multipart-rs-metadata change in CSCvc94318
and is not supported.

---

## 1 — Prerequisites

| | |
|---|---|
| CUBE software | IOS-XE 17.6.5+ recommended; 17.13.1a tested |
| License | CUBE Premium (`license boot level network-advantage addon dna-advantage`) — required for SIPREC |
| LINDA SRS reachability | Public TCP/5061 (TLS) and the negotiated UDP RTP range (default 16384-32768) reachable from CUBE's outside interface |
| TLS | mTLS recommended; CUBE 17+ requires SIP-TLS for SIPREC by default |
| Tenant onboarding | LINDA admin has POSTed `/admin/integrations/siprec` with the tenant's SBC IP allowlist + `provider=siprec_cisco_cube` |

---

## 2 — TLS trustpoint

CUBE needs a TLS trustpoint that includes:

- LINDA's CA cert (validates the SRS hostname on the outbound TLS handshake).
- CUBE's identity cert (presented when LINDA's profile sets `tls-verify-policy = all`).

```
crypto pki trustpoint LINDA-SRS-CA
  enrollment terminal pem
  revocation-check none
  hash sha256
crypto pki authenticate LINDA-SRS-CA
  ! Paste the LINDA-SRS CA certificate (PEM) when prompted.
```

For mTLS — CUBE presents its own cert:

```
crypto pki trustpoint CUBE-IDENTITY
  enrollment selfsigned
  fqdn cube-edge-1.customer.example.com
  subject-name CN=cube-edge-1.customer.example.com
  rsakeypair CUBE-IDENTITY-KEY 2048
crypto pki enroll CUBE-IDENTITY
crypto pki export CUBE-IDENTITY pem terminal
  ! Send the resulting PEM to your LINDA contact for inclusion in the SRS trust bundle.
```

---

## 3 — Media-recording profile

The `media-recording` profile is the CUBE construct that names the
SRS endpoint and the SIPREC behaviour. Replace the FQDN and source
interface to match your environment.

```
voice class sip-options-keepalive 200
  description LINDA SRS SIP-OPTIONS keepalive
  retry 3
  up-interval 30
  down-interval 60

voice class tenant 200
  tls-profile 200
  bind control source-interface GigabitEthernet0/0/1
  bind media source-interface GigabitEthernet0/0/1

voice class tls-profile 200
  description LINDA SRS TLS
  trustpoint CUBE-IDENTITY
  cn-san validate bidirectional
  cn-san 1 srs.linda.<your-tenant>.example.com
  cipher 30 ECDHE-RSA-AES256-GCM-SHA384

voice class media 200
  media-type audio
  media monitoring 102 disable-recording-for monitored-only
  media bulk-stats

voice class server-group 200
  description LINDA SRS pool
  ipv4 srs.linda.<your-tenant>.example.com session-server-group siprec
  hunt-scheme round-robin

dial-peer voice 9100 voip
  description "Outbound SIPREC fork to LINDA SRS"
  destination-pattern T
  session protocol sipv2
  session target sip-server
  session transport tcp tls
  voice-class sip tenant 200
  voice-class sip options-keepalive profile 200
  voice-class sip srtp-crypto 200
  dtmf-relay rtp-nte
  codec g711ulaw
  no vad

voice class srtp-crypto 200
  crypto 1 AES_CM_128_HMAC_SHA1_80
  crypto 2 AES_CM_128_HMAC_SHA1_32
```

The `crypto N` lines control the offer order CUBE will send in the
SDP. AES_CM_128_HMAC_SHA1_80 first — LINDA's SRS prefers it.

---

## 4 — Recorder profile (the SIPREC trigger)

CUBE's `media-recording` configuration ties a recorder to a dial-peer
and selects the metadata format. RFC 7865 (`siprec`) is the only
metadata format LINDA accepts; do **not** use `cisco` or `network`.

```
media profile recorder 100
  media-type audio
  media-recording 9100
  metadata-format siprec
  ! "both" forks both inbound and outbound legs into the same INVITE.
  ! "inbound" or "outbound" forks only one leg; LINDA prefers "both".
  participants both

media class 100
  recorder profile 100

! Apply the media class to the dial-peers that should be recorded.
dial-peer voice 1000 voip
  media-class 100
  ! ... existing dial-peer config ...
```

---

## 5 — Verification

CUBE-side checks:

```
show voice class media 200
show voice class srtp-crypto 200
show voip rtp connections | inc 'SRTP|sec'
show sip-ua statistics
show call active voice brief | inc SRTP
```

When a recorded call lands:

```
debug ccsip messages
debug voip recorder all
```

You should see one INVITE with `Content-Type: multipart/mixed` and a
boundary, and a 200 OK from LINDA's SRS within 200 ms.

LINDA-side checks (replace `:tenant:` with the tenant id):

```
curl -H "Authorization: Bearer $LINDA_ADMIN_TOKEN" \
  https://api.linda.example.com/api/v1/admin/integrations/siprec/sessions?limit=10
```

The most recent row's `provider` should be `siprec_cisco_cube` and
`sdp_crypto_suite` should be `AES_CM_128_HMAC_SHA1_80`.

---

## 6 — Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| 415 Unsupported Media Type | Metadata format is `cisco` or missing | Set `metadata-format siprec` |
| 488 Not Acceptable | LINDA's SRS doesn't accept the offered crypto suite | Verify `voice class srtp-crypto 200` is in the offer chain |
| TLS handshake fails | mTLS cert chain mismatch | Re-export CUBE's cert to LINDA; verify `cn-san` matches the cert subject |
| Audio silent in transcripts | One-way SRTP key error | Confirm `participants both` and inspect `show voip rtp connections` for `secure=yes` on both legs |
| INVITE storms | OPTIONS keepalive too aggressive | Set `up-interval` to 60+ seconds |

---

## 7 — Cisco bug references

- CSCvc94318 — multipart-rs-metadata fix (16.x → 17.x).
- CSCvm54422 — DTLS-SRTP support (17.6.5+; older 17.x falls back to SDES).
- CSCwa45123 — SIPREC pause/resume flag handling for PCI compliance.

If you're seeing unexplained 488s on otherwise-correct configs, search
the Cisco BugTool for these IDs first.
