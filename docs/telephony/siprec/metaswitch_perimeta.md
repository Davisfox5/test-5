# Metaswitch CFS + Perimeta — SIPREC SRC configuration

LINDA's Session Recording Server speaks SIPREC (RFC 7866). On the
Metaswitch side, **CFS** (Call Feature Server) versions 9.0.10+ and
**Perimeta SBC** (the edge SBC) speak the SIPREC protocol natively.

This guide is for SP-class deployments where CFS is running as the
softswitch and Perimeta is the edge SBC. Smaller deployments using
just Perimeta in standalone mode follow the Perimeta-only section
at the bottom.

---

## 1 — Prerequisites

| | |
|---|---|
| CFS version | 9.0.10+ (RFC 7865 metadata support added in 9.0.10) |
| Perimeta version | 4.6+ (recommended); SIPREC supported since 4.5 |
| License | CFS Recording feature license; Perimeta "Enterprise" SKU |
| LINDA SRS reachability | Public TCP/5061 (TLS) and the negotiated UDP RTP range from Perimeta's outside interface |
| TLS | SDES with AES_256 is the Metaswitch default; DTLS-SRTP supported in Perimeta 4.7+ |
| Tenant onboarding | LINDA admin has POSTed `/admin/integrations/siprec` with the tenant's SBC IP allowlist + `provider=siprec_metaswitch` |

---

## 2 — CFS: Recording Server profile

On CFS (via the Metaswitch Provisioning Console or `cli`):

```
profile "LINDA-SRS"
  description "LINDA SIPREC SRS"
  type recording-server
  protocol siprec
  metadata-format rfc7865
  primary-address srs.linda.<tenant>.example.com
  primary-port 5061
  transport tls
  tls-profile LINDA-SRS-TLS
  options-keepalive enabled
  options-keepalive-interval 60
  fail-mode open
end
```

Then the TLS profile:

```
tls-profile LINDA-SRS-TLS
  trust-bundle linda-ca.pem        # uploaded via the EMS UI
  client-cert  perimeta-id.pem
  client-key   perimeta-id.key
  cipher-suite tlsv1.2-secure
  verify-peer  required
end
```

---

## 3 — CFS: Recording profile

```
recording-profile "LINDA-SIPREC"
  recording-server LINDA-SRS
  trigger-event call-answered
  inactive-on-failover false
  capture inbound outbound
  metadata-include-from true
  metadata-include-to   true
  metadata-include-charge-num true
  pci-pause-resume      true
  archive-mode          none      # LINDA is the archive
end
```

Apply the profile to a class of service:

```
service-class "Recorded-Agents"
  recording-profile LINDA-SIPREC
  ! ... existing service-class config ...
end
```

Then assign agents/extensions to the `Recorded-Agents` class via the
provisioning UI. Bulk assignment via CSV is supported in CFS 9.0.13+.

---

## 4 — Perimeta SBC: pass-through

Perimeta sits between CFS and the public network. It needs to permit
the SIPREC fork and route it correctly.

```
sip-router "LINDA-SIPREC-Route"
  match destination-uri ".*@srs.linda.<tenant>.example.com$"
  action route-to-trunk-group LINDA-SRS-TG
end

trunk-group "LINDA-SRS-TG"
  outbound-profile LINDA-SRS-Outbound
  realm public-internet
end

outbound-profile "LINDA-SRS-Outbound"
  transport tls
  tls-profile LINDA-SRS-TLS
  rtp-mode active
  srtp required
  srtp-suites aes-256-cm-hmac-sha1-80,aes-cm-128-hmac-sha1-80
  media-stats-collection enabled
end
```

If Perimeta is doing media interworking (e.g. G.711 ↔ G.722
transcoding), add a media policy:

```
media-policy "LINDA-Media"
  codec-allowed pcmu pcma g722
  codec-prefer  pcmu
  packet-time   20ms
end

associate media-policy LINDA-Media to outbound-profile LINDA-SRS-Outbound
```

---

## 5 — Verification

CFS:

```
show recording-profile LINDA-SIPREC
show recording-server LINDA-SRS status     # expect: "Active"
show calls active recording-active          # active recorded calls
```

Perimeta:

```
show trunk-group LINDA-SRS-TG status
show calls active trunk-group LINDA-SRS-TG
show statistics trunk-group LINDA-SRS-TG
```

LINDA:

```
curl -H "Authorization: Bearer $LINDA_ADMIN_TOKEN" \
  https://api.linda.example.com/api/v1/admin/integrations/siprec/sessions?limit=10
```

Most recent row should have `provider="siprec_metaswitch"` and the
SRTP suite should be `AES_256_CM_HMAC_SHA1_80` (the Metaswitch
default).

---

## 6 — Perimeta-only deployments

Smaller deployments without CFS run Perimeta as the SRC directly. The
configuration is the same as **§4** with two additions:

1. The recording trigger moves to Perimeta's `media-recording` block:

```
media-recording "LINDA-SIPREC"
  recording-server LINDA-SRS-TG
  metadata-format rfc7865
  trigger-event call-answered
  capture inbound outbound
end

associate media-recording LINDA-SIPREC to trunk-group <agent-trunk>
```

2. Concurrency is bounded by Perimeta's media license, not CFS's. Plan
   for 2× the active call count: each recorded call adds one extra
   media leg.

---

## 7 — Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `recording-server inactive` | OPTIONS keepalive not getting 200 OK | Check Perimeta's TLS profile + LINDA's SRS access logs |
| Calls succeed but no recording | Trigger-event is `call-setup` instead of `call-answered` | Switch to `call-answered`; `call-setup` fires before SDP is finalized |
| Audio gaps mid-call | Network jitter > 30 ms between Perimeta and LINDA SRS | Verify path latency; consider WAN QoS DSCP marking (EF for SRTP) |
| 401 Unauthorized from SRS | TLS client cert revoked or not in LINDA's trust bundle | Re-upload Perimeta's identity cert via LINDA admin |
| Crypto downgrade alerts | SDES key mismatch on re-INVITE | Verify Perimeta's `srtp-suites` matches LINDA's offer order |

---

## 8 — Metaswitch documentation references

- *Metaswitch CFS 9.0 Recording Configuration Guide*, Chapter 6.
- *Perimeta 4.6 Service Provider Configuration Guide*, §11 (SIPREC).
- *Metaswitch RFC Compliance Statement* — confirms RFC 7865 support
  starting in CFS 9.0.10 and Perimeta 4.5.
- TR-SIPREC-2024-01 — Metaswitch engineering note on DTLS-SRTP
  rollout in Perimeta 4.7.
