# SIPREC Session Recording Server (SRS) sidecar.
#
# Acts as the receiving end of the customer's SBC's SIPREC fork: speaks
# SIPREC (RFC 7866) over SIP, terminates SRTP, and forwards events +
# plaintext audio frames to the LINDA backend at POST /api/v1/siprec/events.
#
# == Architecture ==
#
# The SRS process is FreeSWITCH userspace (1.10.x) wrapped by a small
# Lua dialplan that:
#   1. Accepts INVITEs whose Content-Type is multipart/mixed and contains
#      an application/rs-metadata+xml part.
#   2. Spawns a per-call event thread that POSTs recording.started /
#      recording.stopped / audio.frame events to LINDA.
#   3. Decrypts SRTP using the SDES key material in the SDP (or, for
#      DTLS-SRTP, completes the DTLS handshake on its own).
#
# == Why FreeSWITCH userspace, not rtpengine ==
#
# rtpengine's high-throughput mode requires the rtpengine kernel module
# (iptables xt_RTPENGINE), which is unavailable on Fly's Firecracker
# VMs. rtpengine has a userspace fallback, but that fallback has worse
# CPU efficiency than FreeSWITCH for the same workload, and we already
# run FreeSWITCH artifacts in our build chain. FreeSWITCH userspace is
# therefore the default in this image. Per the multi-stream plan,
# choosing the production media engine is a USER decision — see
# docs/integrations/stream-1-siprec/USER_TODO.md.
#
# == What the user still has to do ==
#
# - Provision TLS certificates for SIP-TLS and DTLS-SRTP and mount them
#   into /etc/freeswitch/tls/ at deploy time (this Dockerfile leaves the
#   directory empty — a fresh image cannot terminate SIP-TLS).
# - Map SBC → tenant via the tenant_map.json that the entrypoint reads
#   from /etc/linda-srs/tenant_map.json (Fly secret-mounted).
# - Set LINDA_BACKEND_URL and SIPREC_SRS_SHARED_SECRET via Fly secrets.
#
# == Local dev / synthetic-fixture test ==
#
# The accompanying docker-compose.yml boots this image alongside a
# stub backend that records the events. CI's siprec acceptance test
# uses the protocol parser directly (see tests/test_siprec_*.py) — the
# image only needs to *boot* in CI, not serve real SIP traffic.

ARG FREESWITCH_VERSION=1.10.11

FROM debian:bookworm-slim AS build

ARG FREESWITCH_VERSION
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        lsb-release \
    && rm -rf /var/lib/apt/lists/*

# SignalWire's FreeSWITCH packages are gated behind a free token. We
# don't bake the token into the image — the build supplies it via
# `--secret id=fs_token,src=...` and the entrypoint installs at runtime
# if needed. The CI pipeline uses the unauthenticated source-install
# path (slower but tokenless) so the image builds without secrets.
#
# For local dev, set FS_PACKAGE_TOKEN at build time to use the
# pre-built debs (faster).
ARG FS_PACKAGE_TOKEN=""
RUN if [ -n "$FS_PACKAGE_TOKEN" ]; then \
      echo "machine freeswitch.signalwire.com login signalwire password $FS_PACKAGE_TOKEN" \
        > /root/.netrc && chmod 600 /root/.netrc \
      && curl --netrc -fsSL https://freeswitch.signalwire.com/repo/deb/debian-release/signalwire-freeswitch-repo.gpg \
        | gpg --dearmor -o /usr/share/keyrings/signalwire-freeswitch-repo.gpg \
      && echo "deb [signed-by=/usr/share/keyrings/signalwire-freeswitch-repo.gpg] https://freeswitch.signalwire.com/repo/deb/debian-release/ bookworm main" \
        > /etc/apt/sources.list.d/freeswitch.list \
      && apt-get update && apt-get install -y --no-install-recommends \
        freeswitch-meta-default \
        freeswitch-mod-audio-fork \
        freeswitch-mod-lua \
        freeswitch-mod-curl \
        freeswitch-mod-sofia \
        freeswitch-mod-rtc \
      && rm -rf /var/lib/apt/lists/* ; \
    else \
      echo "FS_PACKAGE_TOKEN not set — install FreeSWITCH from source at runtime" ; \
      apt-get update && apt-get install -y --no-install-recommends \
        freeswitch \
        freeswitch-mod-lua \
        freeswitch-mod-sofia \
      && rm -rf /var/lib/apt/lists/* ; \
    fi


FROM debian:bookworm-slim AS runtime

ARG FREESWITCH_VERSION
ENV DEBIAN_FRONTEND=noninteractive
ENV LINDA_BACKEND_URL="http://api.internal:8000"
ENV SIPREC_SRS_SHARED_SECRET=""
ENV SIPREC_SRS_SIP_PORT=5061
ENV SIPREC_SRS_RTP_PORT_MIN=16384
ENV SIPREC_SRS_RTP_PORT_MAX=32768

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        jq \
        python3 \
        python3-requests \
    && rm -rf /var/lib/apt/lists/*

# Pull the runtime artifacts from the build stage if we used the
# package install. When FS_PACKAGE_TOKEN is unset (CI smoke-build), the
# build stage installs the lean Debian-bookworm freeswitch package
# already and we copy from there.
COPY --from=build /usr/bin/freeswitch /usr/bin/freeswitch
COPY --from=build /usr/lib/freeswitch /usr/lib/freeswitch
COPY --from=build /usr/share/freeswitch /usr/share/freeswitch
COPY --from=build /etc/freeswitch /etc/freeswitch

# Drop the FreeSWITCH-bundled defaults so we only ship the SIPREC
# profile we wrote.
RUN rm -rf /etc/freeswitch/dialplan/*.xml /etc/freeswitch/sip_profiles/*.xml \
    && mkdir -p /etc/freeswitch/dialplan \
                /etc/freeswitch/sip_profiles \
                /etc/freeswitch/tls \
                /etc/linda-srs

COPY config.json.template /etc/linda-srs/config.json.template
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY siprec_dialplan.xml /etc/freeswitch/dialplan/siprec.xml
COPY siprec_profile.xml /etc/freeswitch/sip_profiles/siprec.xml
COPY siprec_handler.lua /etc/freeswitch/scripts/siprec_handler.lua

RUN chmod +x /usr/local/bin/entrypoint.sh

# SIP-TLS (port assigned by SIPREC_SRS_SIP_PORT) and the RTP port range.
# Kept narrow to make Fly's UDP-services config explicit; production
# deployments may want a much larger range (one even+odd pair per
# concurrent call).
EXPOSE 5061/tcp 5061/udp
EXPOSE 16384-32768/udp

# Healthcheck just confirms the FreeSWITCH event socket is up; the
# /siprec/events round-trip is exercised by the LINDA app's own checks.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS --max-time 3 http://localhost:8021/ || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["freeswitch", "-nonat", "-nf"]
