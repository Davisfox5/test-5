/* LINDA Insights view controller.
 *
 * Three cards on one page:
 *   1. Onboarding progress (which sections are filled, resume/start button).
 *   2. Pending brief suggestions (approve/reject, run-now trigger).
 *   3. Learned playbook (read-only view of tenant_context.playbook_insights).
 *
 * Refreshes when the view is shown. The nav switcher in demo.js dispatches a
 * 'viewChanged' event on window; we listen for 'linda-insights' and refetch.
 */

(function () {
    const API_BASE = window.__CALLSIGHT_API_BASE__ || "/api/v1";
    const ONBOARDING_SECTIONS = [
        { key: "goals",             label: "Goals" },
        { key: "kpis",              label: "KPIs" },
        { key: "strategies",        label: "Strategies" },
        { key: "org_structure",     label: "Org structure" },
        { key: "personal_touches",  label: "Personal touches" },
    ];

    function $(id) { return document.getElementById(id); }

    function escapeHtml(s) {
        return String(s || "").replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
        })[c]);
    }

    function isFilled(section, brief) {
        const value = (brief || {})[section];
        if (value == null) return false;
        if (Array.isArray(value)) return value.length > 0;
        if (typeof value === "object") {
            return Object.values(value).some((v) => {
                if (v == null) return false;
                if (Array.isArray(v)) return v.length > 0;
                if (typeof v === "string") return v.trim() !== "";
                return Boolean(v);
            });
        }
        if (typeof value === "string") return value.trim() !== "";
        return Boolean(value);
    }

    class LindaInsightsController {
        constructor() {
            this.view = $("linda-insights");
            if (!this.view) return;

            this.apiToken = localStorage.getItem("callsight-api-key");
            this._wireButtons();

            window.addEventListener("callsight:viewChanged", (ev) => {
                if (ev.detail && ev.detail.view === "linda-insights") this.refresh();
            });
        }

        headers() {
            return this.apiToken
                ? { Authorization: `Bearer ${this.apiToken}` }
                : {};
        }

        async refresh() {
            if (!this.apiToken) return;
            await Promise.all([
                this._refreshOnboarding(),
                this._refreshSuggestions(),
            ]);
        }

        async _refreshOnboarding() {
            let brief = {};
            try {
                const resp = await fetch(`${API_BASE}/admin/tenant-context`, {
                    headers: this.headers(),
                });
                if (resp.ok) {
                    const data = await resp.json();
                    brief = data.brief || {};
                }
            } catch (err) {
                console.warn("tenant-context load failed", err);
            }

            const completed = ONBOARDING_SECTIONS.filter((s) => isFilled(s.key, brief));
            const progress = $("liOnboardingProgress");
            const fill = $("liOnboardingFill");
            if (progress) {
                progress.textContent = `${completed.length}/${ONBOARDING_SECTIONS.length} sections`;
            }
            if (fill) {
                fill.style.width = `${(completed.length / ONBOARDING_SECTIONS.length) * 100}%`;
            }

            const checklist = $("liOnboardingChecklist");
            if (checklist) {
                checklist.innerHTML = "";
                ONBOARDING_SECTIONS.forEach((section) => {
                    const li = document.createElement("li");
                    li.textContent = section.label;
                    li.setAttribute(
                        "data-complete",
                        isFilled(section.key, brief) ? "true" : "false"
                    );
                    checklist.appendChild(li);
                });
            }

            this._renderPlaybook(brief.playbook_insights || {});
        }

        _renderPlaybook(pb) {
            const el = $("liPlaybook");
            const sample = $("liPlaybookSample");
            if (!el) return;
            el.innerHTML = "";
            if (sample) {
                sample.textContent = pb.sample_size
                    ? `n=${pb.sample_size} · updated ${(pb.last_learned_at || "").split("T")[0]}`
                    : "no data yet";
            }

            const sections = [
                { key: "what_works", label: "What's working" },
                { key: "what_doesnt", label: "What's not working" },
                { key: "top_performing_phrases", label: "Top phrases" },
                { key: "common_failure_modes", label: "Common failure modes" },
            ];
            let anyContent = false;
            sections.forEach(({ key, label }) => {
                const values = pb[key] || [];
                if (!values || values.length === 0) return;
                anyContent = true;
                const wrap = document.createElement("div");
                wrap.className = "li-playbook-section";
                wrap.innerHTML =
                    `<h4>${escapeHtml(label)}</h4>` +
                    `<ul>${values
                        .slice(0, 5)
                        .map((v) => `<li>${escapeHtml(v)}</li>`)
                        .join("")}</ul>`;
                el.appendChild(wrap);
            });

            const handlers = pb.winning_objection_handlers || [];
            if (handlers.length) {
                anyContent = true;
                const wrap = document.createElement("div");
                wrap.className = "li-playbook-section";
                const items = handlers
                    .slice(0, 5)
                    .map((h) => {
                        const obj = (h && h.objection) || "";
                        const ans = (h && h.handler) || "";
                        return `<li><em>${escapeHtml(obj)}</em> → ${escapeHtml(ans)}</li>`;
                    })
                    .join("");
                wrap.innerHTML =
                    `<h4>Winning objection handlers</h4><ul>${items}</ul>`;
                el.appendChild(wrap);
            }

            if (!anyContent) {
                const p = document.createElement("p");
                p.className = "li-playbook-empty";
                p.textContent =
                    "LINDA hasn't learned enough yet. The refiner runs weekly once you have 3+ call outcomes.";
                el.appendChild(p);
            }
        }

        async _refreshSuggestions() {
            const list = $("liSuggestions");
            if (!list) return;

            let suggestions = [];
            try {
                const resp = await fetch(
                    `${API_BASE}/admin/tenant-context/suggestions?status=pending`,
                    { headers: this.headers() }
                );
                if (resp.ok) {
                    const data = await resp.json();
                    suggestions = data.suggestions || [];
                }
            } catch (err) {
                console.warn("suggestions load failed", err);
            }

            list.innerHTML = "";
            if (suggestions.length === 0) {
                const empty = document.createElement("li");
                empty.className = "li-playbook-empty";
                empty.textContent =
                    "No pending suggestions. The agent runs weekly; click 'Run agent now' to force a pass.";
                list.appendChild(empty);
                return;
            }
            suggestions.forEach((s) => list.appendChild(this._suggestionElement(s)));
        }

        _suggestionElement(s) {
            const li = document.createElement("li");
            li.className = "li-suggestion";
            li.dataset.id = s.id;

            const conf = typeof s.confidence === "number"
                ? `${Math.round(s.confidence * 100)}% confident`
                : "";

            let valueHtml;
            if (typeof s.proposed_value === "string") {
                valueHtml = escapeHtml(s.proposed_value);
            } else {
                valueHtml = `<code>${escapeHtml(
                    JSON.stringify(s.proposed_value, null, 2)
                )}</code>`;
            }

            li.innerHTML = `
                <div class="li-suggestion-head">
                    <span class="li-suggestion-section">${escapeHtml(s.section)}${
                        s.path ? ` · ${escapeHtml(s.path)}` : ""
                    }</span>
                    <span class="li-suggestion-confidence">${conf}</span>
                </div>
                <div class="li-suggestion-value">${valueHtml}</div>
                <p class="li-suggestion-rationale">${escapeHtml(s.rationale || "")}</p>
                <div class="li-suggestion-actions">
                    <button type="button" class="li-suggestion-reject">Reject</button>
                    <button type="button" class="li-suggestion-approve">Approve</button>
                </div>
            `;
            li.querySelector(".li-suggestion-approve").addEventListener(
                "click",
                () => this._decide(li, s.id, "approve")
            );
            li.querySelector(".li-suggestion-reject").addEventListener(
                "click",
                () => this._decide(li, s.id, "reject")
            );
            return li;
        }

        async _decide(node, suggestionId, action) {
            if (!this.apiToken) return;
            node.style.opacity = "0.5";
            try {
                const resp = await fetch(
                    `${API_BASE}/admin/tenant-context/suggestions/${encodeURIComponent(
                        suggestionId
                    )}/${action}`,
                    { method: "POST", headers: this.headers() }
                );
                if (!resp.ok) throw new Error(`${action} failed: ${resp.status}`);
                node.remove();
                if (action === "approve") this._refreshOnboarding();
            } catch (err) {
                console.error(err);
                alert(`Couldn't ${action} suggestion: ${err.message}`);
                node.style.opacity = "1";
            }
        }

        _wireButtons() {
            const start = $("liOnboardingStart");
            if (start) {
                start.addEventListener("click", () => this._startInterview());
            }
            const infer = $("liInferNow");
            if (infer) {
                infer.addEventListener("click", () => this._runInferenceNow(infer));
            }
        }

        async _startInterview() {
            if (!this.apiToken) return;
            try {
                const resp = await fetch(`${API_BASE}/onboarding/sessions`, {
                    method: "POST",
                    headers: this.headers(),
                });
                if (!resp.ok) throw new Error(`start failed: ${resp.status}`);
                const data = await resp.json();
                alert(
                    `Interview ${data.status}. LINDA says:\n\n${data.assistant_message || ""}`
                );
            } catch (err) {
                console.error(err);
                alert(err.message);
            }
        }

        async _runInferenceNow(btn) {
            if (!this.apiToken) return;
            btn.disabled = true;
            btn.textContent = "Running…";
            try {
                const resp = await fetch(
                    `${API_BASE}/admin/tenant-context/infer-now?sync=true`,
                    { method: "POST", headers: this.headers() }
                );
                if (!resp.ok) throw new Error(`infer-now failed: ${resp.status}`);
                await this._refreshSuggestions();
            } catch (err) {
                console.error(err);
                alert(err.message);
            } finally {
                btn.disabled = false;
                btn.textContent = "Run agent now";
            }
        }
    }

    window.LindaInsightsController = LindaInsightsController;

    function bootstrap() {
        if (window.lindaInsights) return;
        if (!document.getElementById("linda-insights")) return;
        window.lindaInsights = new LindaInsightsController();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
