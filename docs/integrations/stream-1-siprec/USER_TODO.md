# Stream 1 (SIPREC) — User-only checklist

The autonomous Stream 1 build landed:

- The Python control plane (`backend/app/services/telephony/siprec/`,
  `backend/app/api/siprec.py`) and tests.
- The `SiprecSession` SQLAlchemy model + Alembic migration
  (`siprec_001_initial.py`).
- The FreeSWITCH-based SRS sidecar Dockerfile, config templates,
  dialplan, and entrypoint
  (`backend/app/services/telephony/siprec_srs/`).
- The `fly.toml` `siprec_srs` process declaration with TLS-SIP and
  RTP port-range services.
- Three vendor-specific SBC config templates
  (`docs/telephony/siprec/{cisco_cube, avaya_sbce, metaswitch_perimeta}.md`).

What remains is the work no autonomous agent can do for you:
infrastructure, certificates, vendor coordination, and a final
self-host vs SaaS decision.

---

## 1 — Decide self-host vs SaaS SRS

Stream 1 implements both shapes:

- **Self-hosted (default)** — the FreeSWITCH userspace sidecar in
  `backend/app/services/telephony/siprec_srs/`. Runs as the
  `siprec_srs` process on Fly. CPU-per-call is non-trivial; estimate
  ~25 MHz/call, so a `performance-2x` Fly machine handles ~150
  concurrent calls. Up-size to `performance-4x` for 300+.
- **SaaS SRS** (e.g. Voxida, RecordingService, Twilio Voice Insights's
  SIPREC option) — your bridge becomes a webhook consumer of the SaaS
  provider's recording-completed events. The control plane in
  `backend/app/api/siprec.py` is unchanged; only the sidecar
  (`siprec_srs` process) gets dropped. SaaS providers publish public
  endpoints; you provide LINDA's `/siprec/events` URL + `X-SRS-Token`
  via their admin console.

Decision drivers:

| | Self-hosted | SaaS |
|---|---|---|
| Per-call cost | Fly machine + your engineer time | $0.005-$0.02/min, no infra |
| Time to first customer | ~2 weeks (TLS + first SBC test) | Days |
| Multi-vendor coverage | All three vendors out of the box | Depends — confirm Cisco/Avaya/Metaswitch all certified |
| Audio path | Direct (LINDA → LINDA) | Through SaaS provider's network |
| Compliance posture | You own the chain of custody | SaaS provider is in scope for SOC 2 / HIPAA |

Stream 1's recommendation: self-hosted for >5 customers; SaaS for
the first one or two.

---

## 2 — Pick rtpengine vs FreeSWITCH userspace (self-host only)

The Dockerfile defaults to FreeSWITCH 1.10 because rtpengine's
high-throughput mode requires the rtpengine kernel module
(`xt_RTPENGINE` iptables target), which is unavailable on Fly's
Firecracker VMs. rtpengine has a userspace fallback, but it's slower
than FreeSWITCH for the same workload.

Action items:

- [ ] Confirm FreeSWITCH userspace meets your throughput targets
      (benchmark: see §3 on the synthetic SIPREC test; multiply by
      your peak concurrent-call estimate).
- [ ] If FreeSWITCH doesn't, evaluate moving the SRS off Fly to a host
      where the rtpengine kernel module is available (a dedicated EC2
      or DigitalOcean droplet with iptables access).

---

## 3 — Provision public IP and DNS for the SRS

Fly assigns a per-app IP, but the SBC needs a stable, customer-facing
hostname:

- [ ] Allocate a Fly **dedicated IPv4** (`flyctl ips allocate-v4
      --shared=false --app linda-staging`). Shared IPv4s won't work
      for SIP-TLS because the SBC's TLS SNI matches a hostname, not
      the shared edge.
- [ ] Create the DNS record:
      `srs.linda.<tenant>.example.com  →  <fly-dedicated-ipv4>`.
- [ ] Register a separate hostname per recording tenant if you don't
      want one customer's SBC IP allowlist to overlap with another's.

---

## 4 — Issue and install TLS certificates

LINDA's SRS terminates SIP-TLS and (for Cisco CUBE 17+ / Avaya SBCE
8+) requires mTLS. The Dockerfile's entrypoint expects:

- `/etc/freeswitch/tls/tls.crt` — server identity (matches the SRS
  hostname).
- `/etc/freeswitch/tls/tls.key` — server private key.
- `/etc/freeswitch/tls/ca.crt` — bundle of CAs that signed the
  *customer's* identity certs (i.e. the certs Cisco/Avaya/Metaswitch
  SBCs present when connecting in mTLS mode).

Action items:

- [ ] Issue an SRS server cert (Let's Encrypt + DNS-01 works; the
      hostname must match what the SBC dials).
- [ ] Mount the cert + key as Fly secrets:
      `flyctl secrets set SIPREC_TLS_CERT=@tls.crt SIPREC_TLS_KEY=@tls.key`
      and arrange for the `entrypoint.sh` to write them to disk
      before FreeSWITCH boots (the current entrypoint expects them
      pre-mounted; adjust if you go the secret-env route).
- [ ] Collect each customer's SBC identity cert and merge them into
      `ca.crt`. Rotate when a customer's cert expires (their SBC
      will fail to dial in until you update the bundle).

DTLS-SRTP (Avaya / Cisco 17.6.5+) uses fingerprint-based auth in the
SDP, not the chain in `ca.crt`. The fingerprint is sent in the
INVITE; you don't pre-install anything for it.

---

## 5 — Set required Fly secrets

```
flyctl secrets set \
  SIPREC_SRS_SHARED_SECRET=$(openssl rand -hex 32) \
  LINDA_BACKEND_URL=https://api.linda.example.com \
  --app linda-staging
```

The shared secret is what the SRS includes in `X-SRS-Token` on every
event. Per-tenant secrets get added separately via the admin
endpoint (`POST /admin/integrations/siprec`).

---

## 6 — Build and push the SRS image

The `fly.toml` declares the `siprec_srs` process but Fly uses the
default `Dockerfile` for all processes by default. To use the
SIPREC-specific image:

```
docker build \
  -t registry.fly.io/linda-staging:siprec_srs-$(git rev-parse --short HEAD) \
  -f backend/app/services/telephony/siprec_srs/Dockerfile \
  backend/app/services/telephony/siprec_srs/

docker push registry.fly.io/linda-staging:siprec_srs-<sha>
```

Then deploy with the `--process-group` override:

```
flyctl deploy --process-group siprec_srs \
  --image registry.fly.io/linda-staging:siprec_srs-<sha> \
  --app linda-staging
```

The api/worker/beat groups continue to use the standard Dockerfile;
only `siprec_srs` gets the FreeSWITCH image.

---

## 7 — Build the FreeSWITCH packages with SignalWire token (optional)

The default Dockerfile install path uses Debian's bundled
`freeswitch` package, which lacks `mod_audio_fork` and `mod_curl`.
For production you want SignalWire's full FreeSWITCH packages:

- [ ] Sign up for a free SignalWire account at
      <https://freeswitch.signalwire.com/>.
- [ ] Generate a personal access token.
- [ ] Build the image with the secret:

```
docker build \
  --secret id=fs_token,src=signalwire-token.txt \
  --build-arg FS_PACKAGE_TOKEN="$(cat signalwire-token.txt)" \
  -t linda-siprec-srs:prod \
  -f backend/app/services/telephony/siprec_srs/Dockerfile \
  backend/app/services/telephony/siprec_srs/
```

(The CI smoke build uses the unauthenticated path, which is enough
for the boot-only acceptance check.)

---

## 8 — Build the per-tenant `tenant_map.json`

The SRS resolves `source IP → LINDA tenant_id + provider` via
`/etc/linda-srs/tenant_map.json`. Format:

```json
{
  "entries": [
    {
      "tenant_id": "00000000-0000-0000-0000-000000000001",
      "provider": "siprec_cisco_cube",
      "sbc_ips": ["192.0.2.10", "192.0.2.11"]
    },
    {
      "tenant_id": "00000000-0000-0000-0000-000000000002",
      "provider": "siprec_avaya_sbce",
      "sbc_ips": ["198.51.100.7"]
    }
  ]
}
```

Mount as a Fly secret:

```
flyctl secrets set --app linda-staging \
  SIPREC_TENANT_MAP="$(cat tenant_map.json | base64)"
```

…and adjust the entrypoint to base64-decode + write to
`/etc/linda-srs/tenant_map.json`. (Stream 1's entrypoint expects the
file pre-mounted; choose your secret-management approach and wire it
up here.)

---

## 9 — Per-customer onboarding

For each customer:

- [ ] POST `/admin/integrations/siprec` with their `provider`,
      `sbc_ip_allowlist`, `srtp_profile`, and `consent_attestation=true`.
      Save the returned secret prefix; the full secret is shown only on
      creation.
- [ ] Add the customer's SBC source IPs to `tenant_map.json` and
      redeploy the secret.
- [ ] Send the customer's network team:
  - The SRS hostname (e.g. `srs.linda.<their-tenant>.example.com`).
  - The SIP-TLS port (5061).
  - The vendor-specific config doc:
    `docs/telephony/siprec/cisco_cube.md` (or `avaya_sbce.md` /
    `metaswitch_perimeta.md`).
- [ ] Schedule a joint test call with the customer's network team to
      verify the first INVITE lands and the audio path is bidirectional.
- [ ] Confirm the customer has obtained legal consent for recording
      under their jurisdiction's rules. Set
      `is_consent_attested=true` only after written confirmation.

---

## 10 — Capacity planning

Fly machine sizing is in `fly.toml` at `performance-2x` (4 vCPU /
8 GB), good for ~150 concurrent calls. For larger tenants:

- [ ] Estimate peak concurrent recorded calls.
- [ ] Up-size the `[[vm]]` block for `siprec_srs` accordingly:
  - `performance-1x` → ~75 calls
  - `performance-2x` → ~150 calls
  - `performance-4x` → ~300 calls
  - `performance-8x` → ~600 calls
- [ ] Widen the RTP port range in `fly.toml` if you exceed 16k ports
      (current default: 16384-32768 = 16384 ports = 8192 even/odd
      pairs). Each concurrent call uses 1-2 pairs.
- [ ] Consider running 2+ `siprec_srs` machines behind round-robin
      DNS for redundancy. The control plane is stateless across
      machines (every event re-derives state from the
      `recording_session_id`); the only risk is in-flight RTP that
      a machine restart drops.

---

## 11 — Post-deploy verification

- [ ] `pytest tests/test_siprec_protocol.py tests/test_siprec_bridge.py` ✅ (green in CI).
- [ ] `docker compose -f backend/app/services/telephony/siprec_srs/docker-compose.yml up siprec_srs` boots without errors ✅.
- [ ] `python -c "from backend.app.main import app"` smoke-imports cleanly ✅.
- [ ] `alembic upgrade head` applies `siprec_001_initial` cleanly.
- [ ] Live test: synthetic INVITE via `sipp` against a deployed staging
      SRS produces a `siprec_sessions` row with the right
      `src_session_id`. **Run this before onboarding the first
      customer.**

---

## 12 — Out-of-scope (deferred)

These are *not* part of Stream 1 by design and require a separate
workstream when ready:

- DTLS-SRTP key extraction in Python (`siprec/srtp.py` punts; the
  SRS handles DTLS termination today).
- Real-time pause/resume control surfaced to the agent UI for
  PCI-compliant card-number capture.
- Automatic per-customer hostname provisioning (i.e.
  `srs.<customer>.linda.example.com`) — currently a manual DNS step.
- Multi-region SRS deployment (active-active or active-passive
  failover). Today's `fly.toml` is single-region (`iad`).

When the user is ready, file them as separate planning docs.
