/* Onboarding Interview chat controller.
 *
 * Talks to the onboarding session API (backend/app/api/onboarding.py):
 *   POST /onboarding/sessions         → starts or resumes
 *   POST /onboarding/sessions/{id}/reply    → send a reply
 *   POST /onboarding/sessions/{id}/complete → splice answers into tenant_context
 *   POST /onboarding/sessions/{id}/abandon  → mark abandoned
 *
 * Renders the agent's message bubbles, a user-reply input, a progress
 * checklist in the sidebar, and enables the "Save & apply" button once
 * the agent says we're done (or after every section has some data).
 */

(function () {
    const API_BASE = window.__CALLSIGHT_API_BASE__ || "/api/v1";
    const SECTIONS = [
        { key: "goals",            label: "Goals" },
        { key: "kpis",             label: "KPIs" },
        { key: "strategies",       label: "Strategies" },
        { key: "org_structure",    label: "Org structure" },
        { key: "personal_touches", label: "Personal touches" },
    ];

    function $(id) { return document.getElementById(id); }

    function escapeHtml(s) {
        return String(s || "").replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
        })[c]);
    }

    class OnboardingController {
        constructor() {
            this.view = $("onboarding");
            if (!this.view) return;

            this.apiToken = localStorage.getItem("callsight-api-key");
            this.sessionId = null;
            this.state = null;  // { history, answers, completed_sections, next_section, done }

            window.addEventListener("callsight:viewChanged", (ev) => {
                if (ev.detail && ev.detail.view === "onboarding") this.onViewOpened();
            });

            // Replace the LINDA-Insights "Start / resume" button behaviour
            // so it navigates here instead of popping an alert.
            const start = $("liOnboardingStart");
            if (start) {
                start.addEventListener(
                    "click",
                    (ev) => {
                        ev.preventDefault();
                        if (typeof window.switchView === "function") {
                            window.switchView("onboarding");
                        }
                    },
                    true  // capture → beats the old bootstrap handler
                );
            }

            this._wireComposer();
            this._wireSidebarButtons();
        }

        headers() {
            return this.apiToken
                ? { Authorization: `Bearer ${this.apiToken}` }
                : {};
        }

        async onViewOpened() {
            if (!this.apiToken) {
                this._setHint("Backend not connected — connect your API key in Preferences first.");
                return;
            }
            await this._startOrResume();
        }

        async _startOrResume() {
            this._setHint("Starting interview…");
            try {
                const resp = await fetch(`${API_BASE}/onboarding/sessions`, {
                    method: "POST",
                    headers: this.headers(),
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const body = await resp.json();
                this.sessionId = body.id;
                this._applyServerState(body);
                this._setInputEnabled(body.status === "active");
                this._setHint(
                    body.status === "completed"
                        ? "Interview completed. Changes are applied to your tenant brief."
                        : "Reply to LINDA below. You can pause anytime — come back to resume."
                );
            } catch (err) {
                console.error("onboarding start failed", err);
                this._setHint(`Couldn't load the interview: ${err.message}`);
            }
        }

        _applyServerState(body) {
            this.state = {
                history: body.history || [],
                answers: body.answers || {},
                completed_sections: body.completed_sections || [],
                next_section: body.next_section,
                done: !!body.done,
            };
            this._renderMessages();
            this._renderChecklist();
            this._renderCompleteButton();
        }

        _renderMessages() {
            const list = $("obMessages");
            if (!list) return;
            list.innerHTML = "";
            for (const msg of (this.state.history || [])) {
                const div = document.createElement("div");
                div.className = "ob-bubble";
                div.dataset.role = msg.role === "user" ? "user" : "assistant";
                div.textContent = msg.content || "";
                list.appendChild(div);
            }
            // System hint when we just started with no prior history.
            if ((this.state.history || []).length === 0) {
                const note = document.createElement("div");
                note.className = "ob-system";
                note.textContent = "LINDA is warming up…";
                list.appendChild(note);
            }
            list.scrollTop = list.scrollHeight;
        }

        _appendBubble(role, content, opts = {}) {
            const list = $("obMessages");
            if (!list) return null;
            const div = document.createElement("div");
            div.className = "ob-bubble";
            div.dataset.role = role;
            if (opts.typing) div.dataset.typing = "true";
            div.textContent = content;
            list.appendChild(div);
            list.scrollTop = list.scrollHeight;
            return div;
        }

        _renderChecklist() {
            const ul = $("obChecklist");
            if (!ul) return;
            ul.innerHTML = "";
            const done = new Set(this.state.completed_sections || []);
            const active = this.state.next_section;
            for (const section of SECTIONS) {
                const li = document.createElement("li");
                li.textContent = section.label;
                if (done.has(section.key)) {
                    li.dataset.state = "done";
                } else if (section.key === active) {
                    li.dataset.state = "active";
                }
                ul.appendChild(li);
            }
        }

        _renderCompleteButton() {
            const btn = $("obCompleteBtn");
            if (!btn) return;
            const readyByAgent = !!this.state.done;
            // Also allow completing once all sections have something — lets
            // the user push ahead without waiting for LINDA to declare done.
            const doneCount = (this.state.completed_sections || []).length;
            const readyByCoverage = doneCount >= SECTIONS.length;
            btn.disabled = !(readyByAgent || readyByCoverage);
            btn.title = btn.disabled
                ? "LINDA will enable this once the interview is covered."
                : "Splice the collected answers into your tenant brief.";
        }

        _setHint(text) {
            const el = $("obHint");
            if (el) el.textContent = text;
        }

        _setInputEnabled(enabled) {
            const input = $("obInput");
            const send = $("obSendBtn");
            if (input) input.disabled = !enabled;
            if (send) send.disabled = !enabled;
        }

        _wireComposer() {
            const form = $("obComposer");
            if (!form) return;
            form.addEventListener("submit", async (ev) => {
                ev.preventDefault();
                const input = $("obInput");
                if (!input || !this.sessionId) return;
                const text = input.value.trim();
                if (!text) return;

                // Optimistic render of the user's bubble.
                this._appendBubble("user", text);
                input.value = "";
                this._setInputEnabled(false);
                const pending = this._appendBubble("assistant", "LINDA is thinking…", { typing: true });

                try {
                    const resp = await fetch(
                        `${API_BASE}/onboarding/sessions/${encodeURIComponent(this.sessionId)}/reply`,
                        {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json",
                                ...this.headers(),
                            },
                            body: JSON.stringify({ message: text }),
                        }
                    );
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    const body = await resp.json();
                    if (pending) pending.remove();
                    this._applyServerState(body);
                } catch (err) {
                    console.error("onboarding reply failed", err);
                    if (pending) pending.remove();
                    this._appendBubble("assistant", `⚠️ ${err.message}`);
                } finally {
                    this._setInputEnabled(true);
                    const i = $("obInput");
                    if (i) i.focus();
                }
            });
        }

        _wireSidebarButtons() {
            const complete = $("obCompleteBtn");
            if (complete) {
                complete.addEventListener("click", async () => {
                    if (!this.sessionId) return;
                    complete.disabled = true;
                    complete.textContent = "Saving…";
                    try {
                        const resp = await fetch(
                            `${API_BASE}/onboarding/sessions/${encodeURIComponent(this.sessionId)}/complete`,
                            { method: "POST", headers: this.headers() }
                        );
                        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                        const body = await resp.json();
                        this._setHint(
                            `Saved. Applied: ${(body.applied_keys || []).join(", ") || "nothing new"}.`
                        );
                        this._setInputEnabled(false);
                        this._appendBubble("assistant", "Thanks — your answers are now part of LINDA's tenant brief.");
                    } catch (err) {
                        console.error("onboarding complete failed", err);
                        this._setHint(`Couldn't save: ${err.message}`);
                    } finally {
                        complete.textContent = "Save & apply";
                        this._renderCompleteButton();
                    }
                });
            }

            const abandon = $("obAbandonBtn");
            if (abandon) {
                abandon.addEventListener("click", async () => {
                    if (!this.sessionId) return;
                    if (!confirm("Abandon this interview? You'll start fresh next time.")) {
                        return;
                    }
                    try {
                        await fetch(
                            `${API_BASE}/onboarding/sessions/${encodeURIComponent(this.sessionId)}/abandon`,
                            { method: "POST", headers: this.headers() }
                        );
                    } catch (err) {
                        console.warn("abandon failed", err);
                    }
                    this.sessionId = null;
                    this.state = null;
                    $("obMessages").innerHTML = "";
                    this._renderChecklist();
                    this._setInputEnabled(false);
                    this._setHint("Interview abandoned. Re-open this view to start a new one.");
                });
            }
        }
    }

    window.OnboardingController = OnboardingController;

    function bootstrap() {
        if (window.onboarding) return;
        if (!document.getElementById("onboarding")) return;
        window.onboarding = new OnboardingController();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
