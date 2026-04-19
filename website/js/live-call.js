/**
 * Live-call WebSocket client.
 *
 * Connects to /ws/live/{sessionId} and dispatches message types to the
 * existing Live Call DOM:
 *
 *   - partial  → replace the pending .transcript-entry.partial
 *   - final    → promote pending or append a new .transcript-entry
 *   - coaching → append as a .hint-card.info (Haiku hint, legacy path)
 *   - alert    → prepend to the alert stack with severity styling
 *   - features → update the bottom-metrics strip, throttled to 1 Hz
 *
 * Degrades gracefully when API_CONNECTED is false — static demo HTML
 * stays untouched.  The caller (demo.js switchView) owns the lifecycle:
 * openLiveCall(sessionId) on entry, closeLiveCall() on leave.
 */

(function () {
    "use strict";

    // ── Module state ────────────────────────────────────────────────
    var socket = null;
    var lastFeaturesRender = 0;
    var FEATURES_RENDER_MIN_INTERVAL_MS = 1000;

    // Alert card management.
    var MAX_VISIBLE_ALERTS = 2;
    var ALERT_AUTO_DISMISS_MS = 8000;
    var ALERT_DISMISS_ANIM_MS = 300;

    // Map server alert.kind → CSS severity class on .hint-card.
    var ALERT_KIND_CLASS = {
        cancel_intent: "danger",
        commitment: "success",
        monologue: "warning",
        patience: "warning",
        filler: "info",
        rapport: "info",
    };

    // ── Public API ──────────────────────────────────────────────────

    window.openLiveCall = function (sessionId) {
        if (typeof window.API_CONNECTED !== "undefined" && !window.API_CONNECTED) {
            return; // static demo mode — don't open a socket.
        }
        if (socket && socket.readyState !== WebSocket.CLOSED) {
            return; // already open.
        }
        try {
            var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
            // Base demo host is served from FastAPI; if running the demo
            // off a static host, allow an override via data-api-root.
            var host = window.location.host;
            var url = proto + "//" + host + "/ws/live/" + encodeURIComponent(sessionId);
            socket = new WebSocket(url);
        } catch (err) {
            console.warn("live-call: WebSocket construction failed", err);
            return;
        }

        socket.addEventListener("message", onMessage);
        socket.addEventListener("close", function () { socket = null; });
        socket.addEventListener("error", function (e) {
            console.warn("live-call: WebSocket error", e);
        });
    };

    window.closeLiveCall = function () {
        if (socket) {
            try { socket.close(); } catch (_) { /* noop */ }
            socket = null;
        }
    };

    // ── Message dispatch ────────────────────────────────────────────

    function onMessage(event) {
        var msg;
        try { msg = JSON.parse(event.data); } catch (e) { return; }
        if (!msg || !msg.type) return;

        switch (msg.type) {
            case "partial":  renderPartial(msg); break;
            case "final":    renderFinal(msg); break;
            case "coaching": appendCoachingHint(msg); break;
            case "alert":    appendAlert(msg); break;
            case "features": renderFeatures(msg); break;
            default: /* unknown type — ignore forward-compat */ break;
        }
    }

    // ── Transcript ─────────────────────────────────────────────────

    function transcriptContainer() {
        return document.querySelector('[data-live="transcript"]');
    }

    function renderPartial(msg) {
        var container = transcriptContainer();
        if (!container) return;
        // Keep only one pending partial entry at a time.
        var existing = container.querySelector(".transcript-entry.partial");
        if (!existing) {
            existing = document.createElement("div");
            existing.className = "transcript-entry partial typing";
            container.appendChild(existing);
        }
        existing.innerHTML =
            '<span class="entry-time">' + escapeHtml(formatTimestamp(msg.timestamp)) + "</span>" +
            '<span class="entry-speaker ' + speakerClass(msg.speaker) + '">' +
            escapeHtml(speakerLabel(msg.speaker)) + "</span>" +
            '<span class="entry-text">' + escapeHtml(msg.text || "") + "</span>";
        scrollToBottom(container);
    }

    function renderFinal(msg) {
        var container = transcriptContainer();
        if (!container) return;
        // Promote pending partial if present, else append.
        var entry = container.querySelector(".transcript-entry.partial");
        if (entry) {
            entry.classList.remove("partial", "typing");
        } else {
            entry = document.createElement("div");
            entry.className = "transcript-entry";
            container.appendChild(entry);
        }
        entry.innerHTML =
            '<span class="entry-time">' + escapeHtml(formatTimestamp(msg.timestamp)) + "</span>" +
            '<span class="entry-speaker ' + speakerClass(msg.speaker) + '">' +
            escapeHtml(speakerLabel(msg.speaker)) + "</span>" +
            '<span class="entry-text">' + escapeHtml(msg.text || "") + "</span>";
        scrollToBottom(container);
    }

    function speakerClass(speaker) {
        if (speaker === 0 || speaker === "0") return "agent";
        return "customer";
    }

    function speakerLabel(speaker) {
        if (speaker === 0 || speaker === "0") return "Agent";
        if (speaker === null || speaker === undefined) return "Customer";
        return "Customer";
    }

    // ── Alert stack ────────────────────────────────────────────────

    function alertsContainer() {
        return document.querySelector('[data-live="alerts"]');
    }

    function appendAlert(msg) {
        var container = alertsContainer();
        if (!container) return;
        var severityClass = ALERT_KIND_CLASS[msg.kind] || "info";
        var card = document.createElement("div");
        card.className = "hint-card live-alert " + severityClass;
        card.setAttribute("data-alert-kind", msg.kind || "");
        card.innerHTML =
            '<span class="hint-icon" aria-hidden="true">' + iconForSeverity(severityClass) + "</span>" +
            "<p>" + escapeHtml(msg.message || "") + "</p>";

        // Prepend so newest alerts render at the top.
        container.insertBefore(card, container.firstChild);

        // Cap the number of *live* alert cards visible; dismiss the
        // oldest live alert when we exceed the cap.  Static demo cards
        // don't carry .live-alert so they won't be touched.
        var live = container.querySelectorAll(".hint-card.live-alert");
        for (var i = MAX_VISIBLE_ALERTS; i < live.length; i++) {
            dismissAlert(live[i]);
        }

        // Auto-dismiss after a short window.
        setTimeout(function () { dismissAlert(card); }, ALERT_AUTO_DISMISS_MS);
    }

    function dismissAlert(card) {
        if (!card || !card.parentNode) return;
        card.classList.add("dismissing");
        setTimeout(function () {
            if (card.parentNode) card.parentNode.removeChild(card);
        }, ALERT_DISMISS_ANIM_MS);
    }

    function appendCoachingHint(msg) {
        // Legacy Haiku hint — treat as a low-severity info card.
        appendAlert({ kind: "info", severity: "info", message: msg.hint || "" });
    }

    function iconForSeverity(cls) {
        // Minimal inline SVGs — reuse the stock lightbulb / info / bang shapes.
        if (cls === "danger") {
            return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/></svg>';
        }
        if (cls === "success") {
            return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
        }
        if (cls === "warning") {
            return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.8.8 1 1.8 1 2.3h6c0-.5.2-1.5 1-2.3A7 7 0 0 0 12 2z"/></svg>';
        }
        return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="16" y2="12"/><line x1="12" x2="12.01" y1="8" y2="8"/></svg>';
    }

    // ── Metrics strip ──────────────────────────────────────────────

    function renderFeatures(msg) {
        var now = Date.now();
        if (now - lastFeaturesRender < FEATURES_RENDER_MIN_INTERVAL_MS) {
            return; // server is already throttling, belt-and-suspenders on client.
        }
        lastFeaturesRender = now;

        var strip = document.querySelector('[data-live="metrics-strip"]');
        if (!strip) return;

        // Talk/Listen bar.
        var repPct = Math.round(100 * (msg.rep_talk_pct || 0));
        var custPct = Math.round(100 * (msg.customer_talk_pct || 0));
        var talk = strip.querySelector(".talk-listen-bar .talk-portion");
        var listen = strip.querySelector(".talk-listen-bar .listen-portion");
        if (talk) { talk.style.width = repPct + "%"; talk.textContent = repPct + "%"; }
        if (listen) { listen.style.width = custPct + "%"; listen.textContent = custPct + "%"; }

        // Patience indicator — green/amber/red.
        var patienceEl = strip.querySelector('[data-live-value="patience"]');
        if (patienceEl) {
            patienceEl.classList.remove("good", "warn", "bad", "neutral");
            var p = msg.patience_sec;
            if (p === null || p === undefined) {
                patienceEl.classList.add("neutral");
                patienceEl.textContent = "—";
            } else if (p >= 0.6) {
                patienceEl.classList.add("good");
                patienceEl.textContent = p.toFixed(2) + "s";
            } else if (p >= 0.25) {
                patienceEl.classList.add("warn");
                patienceEl.textContent = p.toFixed(2) + "s";
            } else {
                patienceEl.classList.add("bad");
                patienceEl.textContent = p.toFixed(2) + "s";
            }
        }

        // Filler rate.
        var fillerEl = strip.querySelector('[data-live-value="fillers"]');
        if (fillerEl) {
            fillerEl.textContent = (msg.filler_rate_per_min || 0).toFixed(1);
        }

        // Interactivity (turns/min).
        var interEl = strip.querySelector('[data-live-value="interactivity"]');
        if (interEl) {
            interEl.textContent = (msg.interactivity_per_min || 0).toFixed(1);
        }
    }

    // ── Helpers ────────────────────────────────────────────────────

    function scrollToBottom(el) {
        el.scrollTop = el.scrollHeight;
    }

    function formatTimestamp(ts) {
        // Server sends Unix seconds; render as HH:MM:SS relative to session
        // open would be nicer but we don't track session start here.  Use
        // MM:SS of the last two minutes of the timestamp for brevity.
        if (!ts) return "";
        var d = new Date(ts * 1000);
        var mm = String(d.getMinutes()).padStart(2, "0");
        var ss = String(d.getSeconds()).padStart(2, "0");
        return mm + ":" + ss;
    }

    function escapeHtml(s) {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }
})();
