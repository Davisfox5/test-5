/* Customer brief card controller.
 *
 * Renders LINDA's per-customer dossier on the live-call view and responds to
 * three WebSocket event kinds:
 *
 *   - `sentiment_update` (tier-gated) — updates the live sentiment score +
 *     sparkline dot. Tenants without the live package see a static
 *     historical sparkline loaded from GET /contacts/{id}/sentiment-history.
 *   - `brief_alert` — pops a toast in the alert lane for churn / upsell /
 *     escalation / advocate / sentiment_drop moments. Non-dismissible by
 *     default; auto-fades after 45s.
 *   - `kb_answer` — not handled here (see kb-cards.js).
 *
 * The agent can submit a note inline; the note POSTs to
 * /customers/{id}/notes, which schedules a debounced brief rebuild. Newly
 * added notes are optimistically prepended to the list so the agent sees
 * confirmation instantly.
 */

(function () {
    const API_BASE = window.__CALLSIGHT_API_BASE__ || "/api/v1";
    const ALERT_FADE_MS = 45_000;
    const MAX_SENTIMENT_POINTS = 40;

    function $(id) { return document.getElementById(id); }

    function escapeHtml(s) {
        return String(s || "").replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
        })[c]);
    }

    function confidenceLevel(score) {
        if (typeof score !== "number") return null;
        if (score >= 0.75) return "high";
        if (score >= 0.45) return "medium";
        return "low";
    }

    function confidenceLabel(score) {
        if (typeof score !== "number") return "";
        return `${Math.round(score * 100)}% confidence`;
    }

    function sentimentTone(score) {
        if (typeof score !== "number") return "neutral";
        if (score >= 6.5) return "positive";
        if (score <= 4) return "negative";
        return "neutral";
    }

    function renderList(el, items, empty) {
        if (!el) return;
        el.innerHTML = "";
        if (!items || items.length === 0) {
            const li = document.createElement("li");
            li.className = "cb-empty";
            li.textContent = empty || "No data yet.";
            el.appendChild(li);
            return;
        }
        items.slice(0, 6).forEach((item) => {
            const li = document.createElement("li");
            if (typeof item === "string") {
                li.textContent = item;
            } else if (item && typeof item === "object") {
                // Generic "render a list of key/value pairs" — used for
                // stakeholders and objections.
                const primary = item.name || item.objection || item.title || "";
                const secondary = item.role || item.context || "";
                const tertiary = item.preferences || item.response || "";
                const bits = [primary, secondary, tertiary].filter(Boolean);
                li.textContent = bits.join(" — ");
                if (item.resolved === false) li.classList.add("cb-unresolved");
            }
            el.appendChild(li);
        });
    }

    function setConfidence(field, brief) {
        const spans = document.querySelectorAll(
            `.cb-confidence[data-field="${field}"]`
        );
        const score = (brief.field_confidences || {})[field];
        const level = confidenceLevel(score);
        spans.forEach((span) => {
            if (level) {
                span.setAttribute("data-level", level);
                span.textContent = confidenceLabel(score);
                span.title = `LINDA confidence: ${confidenceLabel(score)}`;
            } else {
                span.removeAttribute("data-level");
                span.textContent = "";
                span.title = "";
            }
        });
    }

    function updateSparkline(svg, points) {
        if (!svg) return;
        svg.innerHTML = "";
        if (!points || points.length === 0) return;
        const clipped = points.slice(-MAX_SENTIMENT_POINTS);
        const w = 120;
        const h = 30;
        // Scores are 0-10, map 10→top (y=4), 0→bottom (y=26).
        const step = clipped.length > 1 ? w / (clipped.length - 1) : w;
        const coords = clipped.map((score, i) => {
            const y = 26 - (Math.max(0, Math.min(10, Number(score))) / 10) * 22;
            return `${(i * step).toFixed(1)},${y.toFixed(1)}`;
        }).join(" ");
        const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
        polyline.setAttribute("points", coords);
        polyline.setAttribute("fill", "none");
        polyline.setAttribute("stroke", "#10B981");
        polyline.setAttribute("stroke-width", "2");
        svg.appendChild(polyline);

        // Last-point dot for live mode.
        const last = clipped[clipped.length - 1];
        const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        const cx = (clipped.length - 1) * step;
        const cy = 26 - (Math.max(0, Math.min(10, Number(last))) / 10) * 22;
        dot.setAttribute("cx", cx.toFixed(1));
        dot.setAttribute("cy", cy.toFixed(1));
        dot.setAttribute("r", "2.5");
        dot.setAttribute("fill", "#10B981");
        svg.appendChild(dot);
    }

    class CustomerBriefController {
        constructor() {
            this.card = $("liveCustomerBrief");
            if (!this.card) return;

            this.apiToken = localStorage.getItem("callsight-api-key");
            this.contactId = null;
            this.customerId = null;
            this.brief = null;
            this.sentimentPoints = [];
            this.sentimentMode = "historical";  // or "live"
            this.alertTimers = new Map();

            this._wireNoteComposer();
        }

        setFeatures(features) {
            if (features && features.live_sentiment) {
                this.sentimentMode = "live";
                const mode = $("cbSentimentMode");
                if (mode) {
                    mode.textContent = "live";
                    mode.setAttribute("data-mode", "live");
                }
            } else {
                this.sentimentMode = "historical";
                const mode = $("cbSentimentMode");
                if (mode) {
                    mode.textContent = "historical";
                    mode.setAttribute("data-mode", "historical");
                }
            }
        }

        async loadForContact(contactId, customerId) {
            this.contactId = contactId || null;
            this.customerId = customerId || null;
            if (!this.apiToken) return;

            const headers = { Authorization: `Bearer ${this.apiToken}` };

            // Load the brief (may be empty for new customers).
            if (this.customerId) {
                try {
                    const resp = await fetch(
                        `${API_BASE}/customers/${encodeURIComponent(this.customerId)}/brief`,
                        { headers }
                    );
                    if (resp.ok) {
                        const data = await resp.json();
                        this.brief = data.brief || {};
                        this.renderBrief();
                    }
                } catch (err) {
                    console.warn("customer brief load failed", err);
                }

                // Load notes.
                try {
                    const resp = await fetch(
                        `${API_BASE}/customers/${encodeURIComponent(this.customerId)}/notes`,
                        { headers }
                    );
                    if (resp.ok) this.renderNotes(await resp.json());
                } catch (err) {
                    console.warn("customer notes load failed", err);
                }
            }

            // Load historical sentiment — only used when not on the live tier.
            if (this.contactId && this.sentimentMode === "historical") {
                try {
                    const resp = await fetch(
                        `${API_BASE}/contacts/${encodeURIComponent(this.contactId)}/sentiment-history`,
                        { headers }
                    );
                    if (resp.ok) {
                        const data = await resp.json();
                        this.sentimentPoints = data.points || [];
                        updateSparkline($("cbSentimentSparkline"), this.sentimentPoints);
                        const last = this.sentimentPoints[this.sentimentPoints.length - 1];
                        const score = $("cbSentimentScore");
                        if (typeof last === "number" && score) {
                            score.textContent = last.toFixed(1);
                            score.setAttribute("data-tone", sentimentTone(last));
                        }
                    }
                } catch (err) {
                    console.warn("sentiment history load failed", err);
                }
            }
        }

        renderBrief() {
            if (!this.brief) return;
            const b = this.brief;

            const badge = $("cbStatusBadge");
            if (badge) {
                const status = b.current_status || "active";
                badge.setAttribute("data-status", status);
                badge.textContent = status.replace(/_/g, " ");
            }

            renderList($("cbStakeholders"), b.stakeholders, "No stakeholders mapped yet.");
            renderList($("cbBestApproaches"), b.best_approaches, "Gathering signal.");
            renderList($("cbAvoid"), b.avoid, "Nothing flagged yet.");
            renderList($("cbChurnSignals"), b.churn_signals, "No active risks.");
            renderList($("cbUpsellSignals"), b.upsell_signals, "No expansion signals yet.");
            renderList($("cbObjections"), b.objections_raised, "No objections recorded.");

            [
                "stakeholders",
                "best_approaches",
                "avoid",
                "churn_signals",
                "upsell_signals",
                "objections_raised",
            ].forEach((field) => setConfidence(field, b));
        }

        renderNotes(notes) {
            const list = $("cbNotesList");
            const count = $("cbNotesCount");
            if (!list) return;
            list.innerHTML = "";
            const items = Array.isArray(notes) ? notes : [];
            items.forEach((n) => list.appendChild(this._noteElement(n)));
            if (count) count.textContent = String(items.length);
        }

        _noteElement(note) {
            const li = document.createElement("li");
            li.className = "cb-note";
            if (note.reviewed_at) li.setAttribute("data-reviewed", "true");
            const ts = (note.created_at || "").split("T")[0];
            li.innerHTML =
                `<span class="cb-note-meta">${escapeHtml(ts)}${
                    note.reviewed_at ? " · reviewed" : " · pending review"
                }</span>${escapeHtml(note.body)}`;
            return li;
        }

        _wireNoteComposer() {
            const form = $("cbNoteForm");
            const input = $("cbNoteInput");
            const submit = form ? form.querySelector(".cb-note-submit") : null;
            if (!form || !input || !submit) return;

            form.addEventListener("submit", async (ev) => {
                ev.preventDefault();
                const body = input.value.trim();
                if (!body || !this.customerId || !this.apiToken) return;
                submit.disabled = true;
                submit.textContent = "Saving…";
                try {
                    const resp = await fetch(
                        `${API_BASE}/customers/${encodeURIComponent(this.customerId)}/notes`,
                        {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json",
                                Authorization: `Bearer ${this.apiToken}`,
                            },
                            body: JSON.stringify({ body }),
                        }
                    );
                    if (!resp.ok) throw new Error(`Save failed (${resp.status})`);
                    const note = await resp.json();
                    const list = $("cbNotesList");
                    if (list) list.insertBefore(this._noteElement(note), list.firstChild);
                    const count = $("cbNotesCount");
                    if (count) {
                        count.textContent = String((parseInt(count.textContent, 10) || 0) + 1);
                    }
                    input.value = "";
                } catch (err) {
                    console.error("note save failed", err);
                    alert(`Couldn't save note: ${err.message}`);
                } finally {
                    submit.disabled = false;
                    submit.textContent = "Save note";
                }
            });
        }

        /* ── WebSocket event handlers ─────────────────────────────────── */

        handleSentimentUpdate(event) {
            if (this.sentimentMode !== "live") return;
            if (typeof event.score === "number") {
                this.sentimentPoints.push(event.score);
                updateSparkline($("cbSentimentSparkline"), this.sentimentPoints);
                const score = $("cbSentimentScore");
                if (score) {
                    score.textContent = event.score.toFixed(1);
                    score.setAttribute("data-tone", sentimentTone(event.score));
                }
            }
        }

        handleBriefAlert(event) {
            const lane = $("cbAlertLane");
            if (!lane) return;
            const node = document.createElement("div");
            node.className = "cb-alert";
            node.setAttribute("data-kind", event.kind || "info");
            node.innerHTML =
                `<span>${escapeHtml(event.message || "")}</span>` +
                `<button type="button" class="cb-alert-dismiss" aria-label="Dismiss">×</button>`;
            lane.prepend(node);
            const timer = setTimeout(() => node.remove(), ALERT_FADE_MS);
            this.alertTimers.set(node, timer);
            node.querySelector(".cb-alert-dismiss").addEventListener("click", () => {
                clearTimeout(this.alertTimers.get(node));
                this.alertTimers.delete(node);
                node.remove();
            });
        }

        /* Unified entry point — integrators call this with every WS message. */
        handleEvent(event) {
            if (!event || typeof event !== "object") return;
            switch (event.type) {
                case "sentiment_update": return this.handleSentimentUpdate(event);
                case "brief_alert":      return this.handleBriefAlert(event);
                default: return;
            }
        }
    }

    window.CustomerBriefController = CustomerBriefController;

    function bootstrap() {
        if (window.customerBrief) return;
        if (!document.getElementById("liveCustomerBrief")) return;
        window.customerBrief = new CustomerBriefController();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
