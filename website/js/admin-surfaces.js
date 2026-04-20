/* Admin-surfaces controller.
 *
 * Four UI pieces in one controller because they share a lifecycle
 * (fetch on view change, admin-only, reuse `callsight-api-key` token):
 *
 *   - Seat reconciliation banner (on LINDA Insights) + modal (global).
 *   - Billing card (tier picker + Stripe customer-id link) in Preferences.
 *   - Twilio manual credentials form in Integrations.
 *   - Follow-up email composer on the Interaction Detail view.
 *
 * All four fail gracefully when the user isn't admin / no backend is
 * connected: elements stay hidden, nothing crashes.
 */

(function () {
    const API_BASE = window.__CALLSIGHT_API_BASE__ || "/api/v1";

    function $(id) { return document.getElementById(id); }
    function apiToken() { return localStorage.getItem("callsight-api-key"); }
    function isAdmin() {
        const me = window.callsightAuth;
        return !!me && me.role === "admin";
    }
    function headers() {
        const t = apiToken();
        return t ? { Authorization: `Bearer ${t}` } : {};
    }
    function escapeHtml(s) {
        return String(s || "").replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
        })[c]);
    }

    // ── Seat reconciliation ─────────────────────────────────────────

    class SeatReconcileController {
        constructor() {
            this.banner = $("seatBanner");
            this.overlay = $("seatModalOverlay");
            if (!this.banner || !this.overlay) return;

            this.reviewBtn = $("seatBannerReview");
            this.closeBtn = $("seatModalClose");
            this.sub = $("seatModalSub");
            this.list = $("seatSuspendedList");

            if (this.reviewBtn) this.reviewBtn.addEventListener("click", () => this.openModal());
            if (this.closeBtn) this.closeBtn.addEventListener("click", () => this.closeModal());
            this.overlay.addEventListener("click", (ev) => {
                if (ev.target === this.overlay) this.closeModal();
            });

            window.addEventListener("callsight:viewChanged", (ev) => {
                if (ev.detail && ev.detail.view === "linda-insights") this.refresh();
            });
        }

        async refresh() {
            if (!isAdmin() || !apiToken()) {
                this.banner.hidden = true;
                return;
            }
            try {
                const resp = await fetch(`${API_BASE}/admin/seat-reconciliation`, {
                    headers: headers(),
                });
                if (!resp.ok) {
                    this.banner.hidden = true;
                    return;
                }
                const data = await resp.json();
                this.state = data;
                this.renderBanner();
            } catch (err) {
                console.warn("seat-reconciliation fetch failed", err);
                this.banner.hidden = true;
            }
        }

        renderBanner() {
            const { pending, active_users, seat_limit, suspended_users } = this.state || {};
            if (!pending || !suspended_users || suspended_users.length === 0) {
                this.banner.hidden = true;
                return;
            }
            this.banner.hidden = false;
            const body = $("seatBannerBody");
            if (body) {
                body.textContent =
                    `${suspended_users.length} user${suspended_users.length === 1 ? "" : "s"} ` +
                    `${suspended_users.length === 1 ? "was" : "were"} suspended by a plan downgrade. ` +
                    `Your plan allows ${seat_limit} active seat${seat_limit === 1 ? "" : "s"}; ` +
                    `currently ${active_users} ${active_users === 1 ? "is" : "are"} active.`;
            }
        }

        openModal() {
            if (!this.state) return;
            this.overlay.hidden = false;
            this.renderModal();
        }

        closeModal() {
            this.overlay.hidden = true;
        }

        renderModal() {
            const { suspended_users, active_users, seat_limit, admin_seat_limit, active_admins } = this.state;
            if (this.sub) {
                this.sub.textContent =
                    `${suspended_users.length} suspended · ` +
                    `${active_users}/${seat_limit} seats active · ` +
                    `${active_admins}/${admin_seat_limit} admin seats active`;
            }

            this.list.innerHTML = "";

            // For the swap picker we need a list of currently-active users.
            // Load on demand — only one fetch per modal open is fine.
            const atCap = active_users >= seat_limit;

            suspended_users.forEach((u) => {
                const li = document.createElement("li");
                li.dataset.userId = u.id;
                li.innerHTML = `
                    <div class="seat-modal-row-head">
                        <strong>${escapeHtml(u.name || u.email)}</strong>
                        <span>${escapeHtml(u.email)} · ${escapeHtml(u.role)}</span>
                    </div>
                    <div class="seat-modal-actions">
                        <select class="seat-swap-picker" ${atCap ? "" : "hidden"}>
                            <option value="">(no swap needed)</option>
                        </select>
                        <button type="button" class="li-primary-btn seat-reactivate-btn">Reactivate</button>
                    </div>
                `;
                this.list.appendChild(li);
                li.querySelector(".seat-reactivate-btn").addEventListener(
                    "click", () => this.reactivate(u.id, li)
                );
            });

            if (atCap) this._populateSwapPickers();
        }

        async _populateSwapPickers() {
            try {
                const resp = await fetch(`${API_BASE}/users`, { headers: headers() });
                if (!resp.ok) return;
                const users = await resp.json();
                const selects = this.list.querySelectorAll(".seat-swap-picker");
                selects.forEach((select) => {
                    users.forEach((u) => {
                        if (!u.is_active) return;
                        const opt = document.createElement("option");
                        opt.value = u.id;
                        opt.textContent = `Swap with: ${u.name || u.email} (${u.role})`;
                        select.appendChild(opt);
                    });
                });
            } catch (err) {
                console.warn("users fetch for swap picker failed", err);
            }
        }

        async reactivate(userId, row) {
            const picker = row.querySelector(".seat-swap-picker");
            const swapId = picker && picker.value ? picker.value : null;
            const btn = row.querySelector(".seat-reactivate-btn");
            btn.disabled = true;
            btn.textContent = "Working…";
            try {
                const resp = await fetch(
                    `${API_BASE}/users/${encodeURIComponent(userId)}/reactivate`,
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json", ...headers() },
                        body: JSON.stringify(
                            swapId ? { suspend_swap_user_id: swapId } : {}
                        ),
                    }
                );
                if (!resp.ok) {
                    const body = await resp.json().catch(() => ({}));
                    throw new Error(body.detail || `HTTP ${resp.status}`);
                }
                row.remove();
                await this.refresh();
                this.renderModal();
                if ((this.state.suspended_users || []).length === 0) {
                    this.closeModal();
                }
            } catch (err) {
                console.error(err);
                alert(`Couldn't reactivate: ${err.message}`);
                btn.disabled = false;
                btn.textContent = "Reactivate";
            }
        }
    }

    // ── Billing (tier picker + Stripe link) ─────────────────────────

    class BillingController {
        constructor() {
            this.panel = $("billingPanel");
            if (!this.panel) return;

            this.select = $("billingTierSelect");
            this.applyBtn = $("billingTierApply");
            this.pill = $("billingTierPill");
            this.caps = $("billingCaps");
            this.stripeId = $("billingStripeId");
            this.linkBtn = $("billingStripeLink");
            this.status = $("billingStatus");

            if (this.applyBtn) this.applyBtn.addEventListener("click", () => this.applyTier());
            if (this.linkBtn) this.linkBtn.addEventListener("click", () => this.linkStripe());

            window.addEventListener("callsight:viewChanged", (ev) => {
                if (ev.detail && ev.detail.view === "preferences") this.refresh();
            });
        }

        async refresh() {
            if (!isAdmin() || !apiToken()) return;
            try {
                const resp = await fetch(`${API_BASE}/admin/tenant-settings`, {
                    headers: headers(),
                });
                if (!resp.ok) return;
                const s = await resp.json();
                this.render(s);
            } catch (err) {
                console.warn("billing fetch failed", err);
            }
        }

        render(settings) {
            const tiers = settings.tier_catalog || [];
            if (this.select) {
                this.select.innerHTML = "";
                tiers.forEach((t) => {
                    const opt = document.createElement("option");
                    opt.value = t.key;
                    opt.textContent = `${t.label} — ${t.seat_limit} seats / ${t.admin_seat_limit} admins`;
                    if (t.key === settings.subscription_tier) opt.selected = true;
                    this.select.appendChild(opt);
                });
            }
            if (this.pill) this.pill.textContent = settings.subscription_tier || "solo";
            if (this.caps) {
                this.caps.textContent =
                    `Current caps: ${settings.seat_limit} total seats · ${settings.admin_seat_limit} admin seats.`;
            }
        }

        _setStatus(state, msg) {
            if (!this.status) return;
            this.status.textContent = msg || "";
            if (state) this.status.setAttribute("data-state", state);
            else this.status.removeAttribute("data-state");
        }

        async applyTier() {
            if (!this.select) return;
            const tier = this.select.value;
            this._setStatus("", "Applying…");
            try {
                const resp = await fetch(`${API_BASE}/admin/tenant-settings/tier`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", ...headers() },
                    body: JSON.stringify({ tier }),
                });
                if (!resp.ok) {
                    const b = await resp.json().catch(() => ({}));
                    throw new Error(b.detail || `HTTP ${resp.status}`);
                }
                const s = await resp.json();
                this.render(s);
                this._setStatus("ok", "Tier applied. Seat reconciliation may be pending.");
                // Refresh the seat banner too — downgrading just auto-suspended folks.
                if (window.seatReconcile) window.seatReconcile.refresh();
            } catch (err) {
                this._setStatus("error", err.message);
            }
        }

        async linkStripe() {
            if (!this.stripeId) return;
            const id = this.stripeId.value.trim();
            if (!id.startsWith("cus_")) {
                this._setStatus("error", "Stripe customer IDs start with 'cus_'.");
                return;
            }
            this._setStatus("", "Linking…");
            try {
                const resp = await fetch(`${API_BASE}/admin/stripe/link`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", ...headers() },
                    body: JSON.stringify({ stripe_customer_id: id }),
                });
                if (!resp.ok) {
                    const b = await resp.json().catch(() => ({}));
                    throw new Error(b.detail || `HTTP ${resp.status}`);
                }
                this._setStatus("ok", "Linked. Future Stripe events will apply automatically.");
            } catch (err) {
                this._setStatus("error", err.message);
            }
        }
    }

    // ── Twilio manual credentials ───────────────────────────────────

    class TwilioCredsController {
        constructor() {
            this.form = $("twilioCredsForm");
            if (!this.form) return;
            this.sidInput = $("twilioSid");
            this.tokenInput = $("twilioToken");
            this.saveBtn = $("twilioCredsSave");
            this.status = $("twilioCredsStatus");

            this.form.addEventListener("submit", (ev) => this.onSubmit(ev));
        }

        _setStatus(state, msg) {
            if (!this.status) return;
            this.status.textContent = msg || "";
            if (state) this.status.setAttribute("data-state", state);
            else this.status.removeAttribute("data-state");
        }

        async onSubmit(ev) {
            ev.preventDefault();
            if (!isAdmin() || !apiToken()) {
                this._setStatus("error", "Admin access required.");
                return;
            }
            const sid = (this.sidInput.value || "").trim();
            const token = (this.tokenInput.value || "").trim();
            if (!sid.startsWith("AC") || !token) {
                this._setStatus("error", "SID starts with AC…; auth token is required.");
                return;
            }
            this.saveBtn.disabled = true;
            this._setStatus("", "Saving…");
            try {
                const resp = await fetch(`${API_BASE}/admin/integrations/twilio`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", ...headers() },
                    body: JSON.stringify({
                        account_sid: sid,
                        auth_token: token,
                    }),
                });
                if (!resp.ok) {
                    const b = await resp.json().catch(() => ({}));
                    throw new Error(b.detail || `HTTP ${resp.status}`);
                }
                this._setStatus("ok", "Saved. Future calls use these credentials.");
                this.tokenInput.value = "";  // don't linger in DOM
            } catch (err) {
                this._setStatus("error", err.message);
            } finally {
                this.saveBtn.disabled = false;
            }
        }
    }

    // ── Follow-up email composer ────────────────────────────────────

    class FollowUpEmailController {
        constructor() {
            this.btn = $("followUpEmailBtn");
            this.overlay = $("followupOverlay");
            if (!this.btn || !this.overlay) return;
            this.form = $("followupForm");
            this.toInput = $("followupTo");
            this.subjectInput = $("followupSubject");
            this.bodyInput = $("followupBody");
            this.status = $("followupStatus");
            this.closeBtn = $("followupClose");

            this.btn.addEventListener("click", () => this.open());
            this.closeBtn.addEventListener("click", () => this.close());
            this.overlay.addEventListener("click", (ev) => {
                if (ev.target === this.overlay) this.close();
            });
            this.form.addEventListener("submit", (ev) => this.onSubmit(ev));

            // Surface the button when the interaction-detail view is shown and
            // has an ``interactionId`` we can send for. demo.js exposes the
            // current interaction id via ``window.currentInteractionId``; we
            // fall back to hidden if absent.
            window.addEventListener("callsight:viewChanged", (ev) => {
                if (ev.detail && ev.detail.view === "interaction-detail") {
                    this.btn.hidden = !window.currentInteractionId;
                }
            });
        }

        async open() {
            const id = window.currentInteractionId;
            if (!id) return;
            this.overlay.hidden = false;
            this._setStatus("", "Loading draft…");
            try {
                const resp = await fetch(
                    `${API_BASE}/interactions/${encodeURIComponent(id)}/follow-up-draft`,
                    { headers: headers() }
                );
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const draft = await resp.json();
                this.toInput.value = draft.suggested_to || "";
                this.subjectInput.value = draft.draft_subject || "";
                this.bodyInput.value = draft.draft_body || "";
                this._setStatus("", "");
            } catch (err) {
                this._setStatus("error", `Couldn't load draft: ${err.message}`);
            }
        }

        close() {
            this.overlay.hidden = true;
        }

        _setStatus(state, msg) {
            if (!this.status) return;
            this.status.textContent = msg || "";
            if (state) this.status.setAttribute("data-state", state);
            else this.status.removeAttribute("data-state");
        }

        async onSubmit(ev) {
            ev.preventDefault();
            const id = window.currentInteractionId;
            if (!id) return;
            const submit = $("followupSend");
            submit.disabled = true;
            this._setStatus("", "Sending…");
            try {
                const resp = await fetch(
                    `${API_BASE}/interactions/${encodeURIComponent(id)}/send-follow-up`,
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json", ...headers() },
                        body: JSON.stringify({
                            to: this.toInput.value,
                            subject: this.subjectInput.value,
                            body: this.bodyInput.value,
                        }),
                    }
                );
                if (!resp.ok) {
                    const b = await resp.json().catch(() => ({}));
                    throw new Error(b.detail || `HTTP ${resp.status}`);
                }
                this._setStatus("ok", "Sent.");
                setTimeout(() => this.close(), 1200);
            } catch (err) {
                this._setStatus("error", err.message);
            } finally {
                submit.disabled = false;
            }
        }
    }

    // ── Bootstrap ───────────────────────────────────────────────────

    function bootstrap() {
        if (window.seatReconcile) return;  // idempotent guard
        window.seatReconcile = new SeatReconcileController();
        window.billing = new BillingController();
        window.twilioCreds = new TwilioCredsController();
        window.followUpEmail = new FollowUpEmailController();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
