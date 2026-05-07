#!/usr/bin/env bash
# SRS sidecar entrypoint — expand the config template, validate the
# environment, then exec FreeSWITCH.
set -euo pipefail

CONFIG_TEMPLATE="/etc/linda-srs/config.json.template"
CONFIG_OUT="/etc/linda-srs/config.json"
TENANT_MAP="/etc/linda-srs/tenant_map.json"

require_env() {
  local var="$1"
  if [[ -z "${!var:-}" ]]; then
    echo "[siprec_srs] ERROR: required env var $var is unset" >&2
    exit 1
  fi
}

require_env LINDA_BACKEND_URL
require_env SIPREC_SRS_SHARED_SECRET
: "${SIPREC_SRS_SIP_PORT:=5061}"
: "${SIPREC_SRS_RTP_PORT_MIN:=16384}"
: "${SIPREC_SRS_RTP_PORT_MAX:=32768}"
: "${SIPREC_FLY_PRIVATE_IP:=$(hostname -i 2>/dev/null | awk '{print $1}' || echo '0.0.0.0')}"
export SIPREC_SRS_SIP_PORT SIPREC_SRS_RTP_PORT_MIN SIPREC_SRS_RTP_PORT_MAX SIPREC_FLY_PRIVATE_IP

# Expand env-var placeholders. ``envsubst`` is more correct than sed for
# this — it skips anything that isn't a known variable, so a template
# typo doesn't silently produce a broken JSON file.
if command -v envsubst >/dev/null 2>&1; then
  envsubst < "$CONFIG_TEMPLATE" > "$CONFIG_OUT"
else
  python3 -c '
import os, sys
src = open("'"$CONFIG_TEMPLATE"'").read()
for k, v in os.environ.items():
    src = src.replace("${" + k + "}", v)
open("'"$CONFIG_OUT"'", "w").write(src)
'
fi

# tenant_map.json is the source of truth for SBC IP → LINDA tenant
# resolution. It's mounted as a Fly secret at runtime; missing-file is
# fatal in production but allowed in CI smoke-builds (controlled by
# SIPREC_SRS_REQUIRE_TENANT_MAP).
if [[ ! -f "$TENANT_MAP" ]]; then
  if [[ "${SIPREC_SRS_REQUIRE_TENANT_MAP:-true}" == "true" ]]; then
    echo "[siprec_srs] ERROR: tenant_map.json missing at $TENANT_MAP. " \
         "Mount as a Fly secret before deploying. Set " \
         "SIPREC_SRS_REQUIRE_TENANT_MAP=false to bypass for smoke-tests." >&2
    exit 1
  else
    echo "[siprec_srs] WARN: tenant_map.json missing — SRS will reject all SBCs." >&2
    echo '{}' > "$TENANT_MAP"
  fi
fi

# TLS cert sanity-check. SIP-TLS is required by Cisco CUBE 17+ and
# Avaya SBCE 8+; rather than letting FreeSWITCH boot without certs and
# fail on the first INVITE, fail fast.
if [[ ! -s /etc/freeswitch/tls/tls.crt ]] && [[ "${SIPREC_SRS_ALLOW_NO_TLS:-false}" != "true" ]]; then
  echo "[siprec_srs] ERROR: /etc/freeswitch/tls/tls.crt is missing or empty. " \
       "Provision per docs/integrations/stream-1-siprec/USER_TODO.md " \
       "or set SIPREC_SRS_ALLOW_NO_TLS=true for plaintext-SIP smoke tests." >&2
  exit 1
fi

echo "[siprec_srs] config rendered → $CONFIG_OUT"
echo "[siprec_srs] backend = $LINDA_BACKEND_URL"
echo "[siprec_srs] SIP port = $SIPREC_SRS_SIP_PORT (TLS)"
echo "[siprec_srs] RTP range = $SIPREC_SRS_RTP_PORT_MIN-$SIPREC_SRS_RTP_PORT_MAX"

exec "$@"
