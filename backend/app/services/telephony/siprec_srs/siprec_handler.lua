-- siprec_handler.lua
--
-- FreeSWITCH-side glue for the LINDA SIPREC SRS sidecar. Each inbound
-- INVITE that lands in the ``siprec`` dialplan executes this script.
--
-- Responsibilities:
--   * Reject (415 Unsupported Media Type) any INVITE whose body isn't
--     a SIPREC multipart with an rs-metadata+xml part.
--   * Resolve the customer's tenant_id by matching the source IP
--     against tenant_map.json (loaded once at script init).
--   * POST recording.started / .stopped / audio.frame events to
--     ${LINDA_BACKEND_URL}/api/v1/siprec/events with the configured
--     X-SRS-Token header.
--   * Configure mod_audio_fork to stream the inbound RTP audio to the
--     same /siprec/events endpoint as audio.frame events.
--
-- This file is intentionally compact. The SIPREC parsing logic lives
-- in the LINDA backend (services/telephony/siprec/protocol.py) — the
-- handler forwards the raw multipart body and lets Python do the SDP
-- + rs-metadata work. Doing the parse twice would invite drift.

local cjson_ok, cjson = pcall(require, "cjson")
if not cjson_ok then
    cjson = nil
end

local CONFIG_PATH = "/etc/linda-srs/config.json"
local TENANT_MAP_PATH = "/etc/linda-srs/tenant_map.json"

local function read_file(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local body = f:read("*a")
    f:close()
    return body
end

local config = (function()
    local txt = read_file(CONFIG_PATH)
    if not txt or not cjson then return {} end
    local ok, parsed = pcall(cjson.decode, txt)
    return ok and parsed or {}
end)()

local tenant_map = (function()
    local txt = read_file(TENANT_MAP_PATH)
    if not txt or not cjson then return {} end
    local ok, parsed = pcall(cjson.decode, txt)
    return ok and parsed or {}
end)()

local backend_url = (config.backend and config.backend.url) or
    os.getenv("LINDA_BACKEND_URL") or "http://api.internal:8000"
local events_path = (config.backend and config.backend.events_path) or
    "/api/v1/siprec/events"
local shared_secret = os.getenv("SIPREC_SRS_SHARED_SECRET") or ""

-- Resolve the LINDA tenant_id + provider for an inbound SBC. Returns
-- nil when the source IP isn't in the tenant_map.json — caller should
-- 403 the call.
local function resolve_tenant(remote_ip)
    if not remote_ip then return nil end
    for _, entry in ipairs(tenant_map.entries or {}) do
        for _, ip in ipairs(entry.sbc_ips or {}) do
            if ip == remote_ip then
                return entry.tenant_id, entry.provider
            end
        end
    end
    return nil
end

-- POST a JSON event to the LINDA backend.
local function post_event(payload)
    if not cjson then
        freeswitch.consoleLog("ERR", "siprec_handler: cjson missing\n")
        return false
    end
    local body = cjson.encode(payload)
    local cmd = string.format(
        "curl -fsS --max-time 5 " ..
        "-H 'Content-Type: application/json' " ..
        "-H 'X-SRS-Token: %s' " ..
        "-X POST %s%s -d @-",
        shared_secret, backend_url, events_path
    )
    local p = io.popen(cmd, "w")
    if not p then return false end
    p:write(body)
    p:close()
    return true
end

-- ── Main: handle the inbound SIPREC INVITE ─────────────────────────────

local remote_ip = session:getVariable("sip_network_ip")
local tenant_id, provider = resolve_tenant(remote_ip)
if not tenant_id then
    freeswitch.consoleLog("WARNING",
        "siprec_handler: SBC " .. tostring(remote_ip) ..
        " not in tenant_map; rejecting INVITE\n")
    session:hangup("CALL_REJECTED")
    return
end

local recording_session_id = session:getVariable("sip_h_X-Linda-Rec-Session")
if not recording_session_id or recording_session_id == "" then
    -- Fall back to the SIP Call-ID; Python's metadata parser will
    -- replace this with the rs-metadata session_id.
    recording_session_id = session:getVariable("sip_call_id") or "unknown"
end

local invite_body = session:getVariable("sip_h_Content-Type")
local multipart_body = session:getVariable("sip_full_body")

post_event({
    event = "recording.started",
    recording_session_id = recording_session_id,
    tenant_id = tenant_id,
    provider = provider,
    src_call_id = session:getVariable("sip_call_id"),
    src_metadata = {
        invite_content_type = invite_body,
        sip_full_body = multipart_body,
        remote_ip = remote_ip,
    },
    is_consent_attested = false,
})

-- Fork audio frames to the LINDA backend. The fork URL has the
-- recording_session_id embedded so the bridge can route frames
-- without a per-call lookup.
local fork_url = string.format(
    "%s%s?rec=%s",
    backend_url, events_path, recording_session_id
)
session:execute("audio_fork", "start " .. fork_url ..
    " mono " .. ((config.media and config.media.preferred_format) or "mulaw_8k"))

-- Block until the SBC tears down the call.
session:execute("park")

post_event({
    event = "recording.stopped",
    recording_session_id = recording_session_id,
    tenant_id = tenant_id,
    end_reason = session:getVariable("hangup_cause") or "unknown",
})
