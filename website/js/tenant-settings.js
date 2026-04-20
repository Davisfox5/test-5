/* Tenant settings panel controller.
 *
 * Loads GET /admin/tenant-settings on view enter, renders toggle rows from
 * the server-provided `feature_flag_spec` (so labels/help stay in one place),
 * and PATCHes updates as the user edits. Status pill top-right shows
 * Saving… / Saved / Error.
 *
 * Designed to fail gracefully: with no API token or the backend unavailable,
 * the panel just shows "Backend not connected" and the rest of Preferences
 * still works.
 */

(function () {
    const API_BASE = window.__CALLSIGHT_API_BASE__ || "/api/v1";
    const DEBOUNCE_MS = 500;

    function $(id) { return document.getElementById(id); }

    function escapeHtml(s) {
        return String(s || "").replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
        })[c]);
    }

    class TenantSettingsController {
        constructor() {
            this.panel = $("tenantSettingsPanel");
            if (!this.panel) return;

            this.apiToken = localStorage.getItem("callsight-api-key");
            this.settings = null;
            this._saveTimer = null;

            window.addEventListener("callsight:viewChanged", (ev) => {
                if (ev.detail && ev.detail.view === "preferences") this.load();
            });

            // Kick off a load if the view is already active (e.g. if the
            // user lands directly on Preferences).
            if (document.querySelector("#preferences.view.active")) this.load();
        }

        headers() {
            return this.apiToken
                ? { Authorization: `Bearer ${this.apiToken}` }
                : {};
        }

        setStatus(state, text) {
            const el = $("tenantSettingsStatus");
            if (!el) return;
            el.textContent = text;
            if (state) el.setAttribute("data-state", state);
            else el.removeAttribute("data-state");
        }

        async load() {
            if (!this.apiToken) {
                this.setStatus("error", "Backend not connected");
                return;
            }
            this.setStatus("", "Loading…");
            try {
                const resp = await fetch(`${API_BASE}/admin/tenant-settings`, {
                    headers: this.headers(),
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                this.settings = await resp.json();
                this.render();
                this.setStatus("saved", "Up to date");
            } catch (err) {
                console.warn("tenant-settings load failed", err);
                this.setStatus("error", "Couldn't load settings");
            }
        }

        render() {
            if (!this.settings) return;
            const { settings } = this;

            // Feature flag rows — one per entry in feature_flag_spec.
            const list = $("featureFlagList");
            if (list) {
                list.innerHTML = "";
                (settings.feature_flag_spec || []).forEach((spec) => {
                    const current = Boolean(
                        (settings.features_enabled || {})[spec.key]
                    );
                    list.appendChild(this._renderFlagRow(spec, current));
                });
            }

            // Scalar fields.
            const engine = $("tenantTranscriptionEngine");
            if (engine) {
                engine.value = settings.transcription_engine || "deepgram";
                engine.onchange = () =>
                    this._save({ transcription_engine: engine.value });
            }
            const auto = $("tenantAutomationLevel");
            if (auto) {
                auto.value = settings.automation_level || "approval";
                auto.onchange = () =>
                    this._save({ automation_level: auto.value });
            }
            const lang = $("tenantDefaultLanguage");
            if (lang) {
                lang.value = settings.default_language || "en";
                lang.oninput = () =>
                    this._saveDebounced({ default_language: lang.value });
            }

            const boost = $("tenantKeytermBoost");
            if (boost) {
                boost.value = (settings.keyterm_boost_list || []).join(", ");
                boost.oninput = () =>
                    this._saveDebounced({
                        keyterm_boost_list: _splitList(boost.value),
                    });
            }
            const q = $("tenantQuestionKeyterms");
            if (q) {
                q.value = (settings.question_keyterms || []).join(", ");
                q.oninput = () =>
                    this._saveDebounced({
                        question_keyterms: _splitList(q.value),
                    });
            }
        }

        _renderFlagRow(spec, checked) {
            const row = document.createElement("div");
            row.className = "feature-flag-row";
            row.innerHTML = `
                <div class="feature-flag-copy">
                    <span class="feature-flag-label">${escapeHtml(spec.label)}</span>
                    <span class="feature-flag-help">${escapeHtml(spec.help || "")}</span>
                </div>
                <label class="toggle-switch">
                    <input type="checkbox" ${checked ? "checked" : ""}
                        data-flag="${escapeHtml(spec.key)}">
                    <span class="toggle-slider"></span>
                </label>
            `;
            row.querySelector("input[type=checkbox]").addEventListener(
                "change",
                (ev) => {
                    const key = ev.target.getAttribute("data-flag");
                    const value = ev.target.checked;
                    this._save({ features_enabled: { [key]: value } });
                }
            );
            return row;
        }

        _saveDebounced(patch) {
            if (this._saveTimer) clearTimeout(this._saveTimer);
            this._saveTimer = setTimeout(() => this._save(patch), DEBOUNCE_MS);
        }

        async _save(patch) {
            if (!this.apiToken) return;
            this.setStatus("saving", "Saving…");
            try {
                const resp = await fetch(`${API_BASE}/admin/tenant-settings`, {
                    method: "PATCH",
                    headers: {
                        "Content-Type": "application/json",
                        ...this.headers(),
                    },
                    body: JSON.stringify(patch),
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                this.settings = await resp.json();
                // Don't re-render on every save — keeps focus/cursor in
                // text inputs. We only replace the in-memory snapshot.
                this.setStatus("saved", "Saved");
            } catch (err) {
                console.error("tenant-settings save failed", err);
                this.setStatus("error", "Save failed — retrying on next change");
            }
        }
    }

    function _splitList(raw) {
        return String(raw || "")
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
    }

    window.TenantSettingsController = TenantSettingsController;

    function bootstrap() {
        if (window.tenantSettings) return;
        if (!document.getElementById("tenantSettingsPanel")) return;
        window.tenantSettings = new TenantSettingsController();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
