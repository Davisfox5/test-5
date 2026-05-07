# Avaya SBCE — SIPREC SRC configuration

LINDA's Session Recording Server speaks SIPREC (RFC 7866). On the
Avaya side, **SBCE** (Session Border Controller for Enterprise)
versions 8.x and 10.x are the supported SRCs. Earlier versions (7.x)
use a non-RFC-compliant variant; upgrade before integrating.

This guide covers the SBCE EMS web UI flow (most customers' workflow);
CLI references are footnoted where the UI doesn't expose a setting.

---

## 1 — Prerequisites

| | |
|---|---|
| SBCE version | 8.1 SP4+ recommended; 10.1.2 tested |
| License | "Advanced Services" or "Recording" SKU; consult your Avaya rep |
| Aura/CM side | Communication Manager 8.0+; recording route already terminates at SBCE (Service Provider profile, not Endpoint Recording) |
| LINDA SRS reachability | Public TCP/5061 (TLS) and the negotiated UDP RTP range from SBCE's external interface |
| TLS | DTLS-SRTP is the default in 10.x; LINDA's SRS supports both DTLS and SDES |
| Tenant onboarding | LINDA admin has POSTed `/admin/integrations/siprec` with the tenant's SBC IP allowlist + `provider=siprec_avaya_sbce` |

---

## 2 — TLS trust setup

EMS → **Device Specific Settings → TLS Management → Certificates**

Upload three certificates:

1. **Trusted Root** — LINDA SRS's root CA (PEM format).
2. **Identity Cert** — SBCE's own server cert; must include the SBCE's
   public FQDN as a SAN.
3. **CA Bundle** — any intermediate CAs that signed your identity cert.

EMS → **TLS Profiles → Server Profile → Add**:

```
Profile Name:           LINDA-SRS-Server
Certificate:            <SBCE Identity Cert>
Trust Store:            <LINDA SRS Root>
Peer Verification:      Required
Peer Certificate Authentication: Required
TLS Version:            TLS 1.2 (TLS 1.3 if SBCE 10.1+)
```

EMS → **TLS Profiles → Client Profile → Add** with the same
certificate bundle but a different name (`LINDA-SRS-Client`).

---

## 3 — SIPREC server entry

EMS → **Global Profiles → Recording Server → Add**:

```
Server Name:        LINDA-SRS
Address:            <FQDN of LINDA SRS, e.g. srs.linda.<tenant>.example.com>
Port:               5061
Transport:          TLS
TLS Profile:        LINDA-SRS-Client
Recording Mode:     SIPREC
Metadata Mode:      RFC 7865
Active:             Yes
Heartbeat:          OPTIONS, 60s
Failover Server:    (optional second SRS for HA)
```

---

## 4 — Recording Profile

EMS → **Domain Policies → Recording Profile → Add**:

```
Profile Name:           LINDA-SIPREC
Recording Server Group: LINDA-SRS  (created above)
Recording Type:         Persistent (full-call) — or "On-Demand" if you
                        want PCI pause/resume control from the agent
Failure Behavior:       Fail Open  (call continues if SRS is unreachable;
                        change to "Fail Closed" only if compliance
                        requires it)
Send Pause/Resume:      Enabled  (PCI scenarios)
```

---

## 5 — Media Forking (Mediation Server profile)

The recording happens at the Mediation Server level; SBCE forks media
into the SRS while keeping the original call legs intact.

EMS → **Network & Flows → Server Configuration → Mediation Server**:

For the **outbound** profile that points at LINDA's SRS, set:

```
Profile Name:               LINDA-Mediation
Connect Timeout:            5 seconds
Heartbeat:                  Enabled, 60s
Max Concurrent Forks:       <license limit; usually 250+>
Adaptive Concurrency:       Enabled
Codecs Allowed:             G.711 μ-law, G.711 A-law, G.722
                            (LINDA normalizes to μ-law 8 kHz; G.722
                             gets downsampled, no quality loss for
                             telephony)
SRTP:                       Required (DTLS-SRTP preferred)
Crypto Suites:              AEAD_AES_256_GCM_8, AES_256_CM_HMAC_SHA1_80,
                            AES_CM_128_HMAC_SHA1_80
```

EMS → **Domain Policies → Application Rules**: bind the
`LINDA-SIPREC` recording profile to the application rule that handles
the recorded population (typically your "Aura → Trunk" rule).

---

## 6 — Subscriber assignment

The recording is attached to a "subscriber" in SBCE terms. Subscribers
are the individual users / DIDs / extension ranges whose calls get
forked.

EMS → **Subscribers → Add → Recording Profile = LINDA-SIPREC**:

- For per-extension recording: add each extension as a subscriber.
- For DID-range recording: add a subscriber pattern (`+1212555*`).
- For 100% recording: assign at the trunk level (Application Rule).

---

## 7 — Verification

SBCE EMS:

- **Status → Server Status → Recording Servers** — `LINDA-SRS` should
  show *Active*.
- **Trace → Call Trace** — start a test call from a subscribed
  extension; the trace should show one outbound INVITE to LINDA-SRS
  with `Content-Type: multipart/mixed`.

CLI (`sftp` to SBCE then `tail -f /usr/local/avaya/sbcc/log/siprec.log`):

```
[2026-05-07 14:35:10.243] SIPREC: INVITE → 198.51.100.1:5061 → 200 OK (231ms)
[2026-05-07 14:35:10.510] SIPREC: SDP negotiated DTLS-SRTP, AES_256
[2026-05-07 14:35:10.512] SIPREC: forking media (agent + customer)
```

LINDA-side check:

```
curl -H "Authorization: Bearer $LINDA_ADMIN_TOKEN" \
  https://api.linda.example.com/api/v1/admin/integrations/siprec/sessions?limit=10
```

Most recent row should have `provider="siprec_avaya_sbce"` and
`sdp_crypto_suite="DTLS_SRTP"` (or the negotiated SDES suite).

---

## 8 — Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| 503 Service Unavailable from SRS | LINDA SRS overloaded or in maintenance | Failover server should kick in; if not, check the heartbeat OPTIONS reply latency |
| TLS handshake aborted | SBCE's identity cert SAN doesn't match the FQDN it presents | Reissue the identity cert with the correct SAN |
| Audio one-way | DTLS-SRTP fingerprint mismatch (cert rotated mid-call) | Restart the call; SBCE caches DTLS state per dialog |
| Recordings missing for some users | Subscriber not bound to the recording profile | Check **Subscribers** for the user's extension |
| Pause/resume not respected | Application Rule has `Send Pause/Resume = Disabled` | Enable; verify with `tail -f siprec.log` during a test |

---

## 9 — Avaya documentation references

- *Avaya SBCE 10.1 Administration Guide*, Chapter 14 (Recording).
- PSN020405u (DTLS-SRTP defaults change in 10.1).
- *Recording Server Integration Reference Guide* (Avaya Support
  document ID 109024).
- DRG-SIPREC-001 — Avaya engineering reference for SIPREC RFC 7865
  metadata generation behavior.
