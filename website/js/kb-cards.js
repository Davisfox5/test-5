/* Live KB card controller.
 *
 * Wired to the /ws/live/{session_id} WebSocket. On each `kb_answer` message we
 * render a card in the sidebar; cards auto-fade after 30s without interaction,
 * and can be pinned / dismissed / expanded. Pinning persists across calls with
 * the same contact via POST /kb/pins.
 */

(function () {
    const AUTO_FADE_MS = 30_000;
    const MAX_VISIBLE_CARDS = 5;
    const API_BASE = window.__CALLSIGHT_API_BASE__ || "/api/v1";

    function escapeHtml(s) {
        return String(s || "").replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
        })[c]);
    }

    function makeCard(event, { onPin, onDismiss, onExpand }) {
        const el = document.createElement("article");
        el.className = "kb-card";
        if (event.pinned) el.classList.add("pinned");
        el.dataset.chunkId = event.chunk_id || "";
        el.dataset.docId = event.doc_id || "";
        if (event.pin_id) el.dataset.pinId = event.pin_id;

        const confidencePct = Math.max(0, Math.min(100, Math.round((event.confidence || 0) * 100)));
        const docTitle = event.doc_title || "Untitled document";
        const sourceUrl = event.source_url || "";

        el.innerHTML = `
            <header class="kb-card-header">
                <div class="kb-card-title">
                    <svg class="kb-doc-icon" width="14" height="14" viewBox="0 0 24 24" fill="none"
                         stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/>
                    </svg>
                    <span class="kb-doc-name">${escapeHtml(docTitle)}</span>
                </div>
                <div class="kb-card-actions">
                    <button type="button" class="kb-pin-btn ${event.pinned ? "pinned" : ""}" title="${event.pinned ? "Unpin" : "Pin (persists across calls)"}">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="${event.pinned ? "currentColor" : "none"}"
                             stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <line x1="12" y1="17" x2="12" y2="22"/>
                            <path d="M5 17h14l-1.68-8.39a2 2 0 0 0-.98-1.38L12 4 7.66 7.23a2 2 0 0 0-.98 1.38L5 17z"/>
                        </svg>
                    </button>
                    <button type="button" class="kb-expand-btn" title="Expand">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                             stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="6 9 12 15 18 9"/>
                        </svg>
                    </button>
                    <button type="button" class="kb-dismiss-btn" title="Dismiss">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                             stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <line x1="18" y1="6" x2="6" y2="18"/>
                            <line x1="6" y1="6" x2="18" y2="18"/>
                        </svg>
                    </button>
                </div>
            </header>
            <p class="kb-card-snippet">${escapeHtml(event.snippet || "")}</p>
            <footer class="kb-card-footer">
                <div class="kb-confidence-bar" title="Similarity: ${confidencePct}%">
                    <div class="kb-confidence-fill" style="width: ${confidencePct}%"></div>
                </div>
                <span class="kb-card-meta">
                    ${confidencePct}%${sourceUrl ? ` · <a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener">source</a>` : ""}
                </span>
            </footer>
        `;

        el.querySelector(".kb-pin-btn").addEventListener("click", (ev) => {
            ev.stopPropagation();
            onPin(el);
        });
        el.querySelector(".kb-expand-btn").addEventListener("click", (ev) => {
            ev.stopPropagation();
            onExpand(el);
        });
        el.querySelector(".kb-dismiss-btn").addEventListener("click", (ev) => {
            ev.stopPropagation();
            onDismiss(el);
        });

        return el;
    }

    class KbCardController {
        constructor({ containerId = "liveKbAnswers", historyId = "kbHistoryDrawer", historyToggleId = "kbHistoryToggle", historyCountId = "kbHistoryCount" } = {}) {
            this.container = document.getElementById(containerId);
            this.historyDrawer = document.getElementById(historyId);
            this.historyToggle = document.getElementById(historyToggleId);
            this.historyCount = document.getElementById(historyCountId);

            this.contactId = null;
            this.apiToken = window.__CALLSIGHT_API_TOKEN__ || null;

            // chunk_id -> { element, timerId }
            this._active = new Map();
            // chunk_id -> { chunk_id, doc_id, pin_id, element }
            this._pinned = new Map();
            this._historyCount = 0;

            if (this.historyToggle && this.historyDrawer) {
                this.historyToggle.addEventListener("click", () => this._toggleHistory());
            }
        }

        setContact(contactId) { this.contactId = contactId || null; }

        /** Call when the WebSocket opens to rehydrate pinned cards for the contact. */
        async rehydratePins(contactId) {
            this.setContact(contactId);
            if (!contactId || !this.apiToken) return;
            try {
                const resp = await fetch(`${API_BASE}/kb/pins?contact_id=${encodeURIComponent(contactId)}`, {
                    headers: { Authorization: `Bearer ${this.apiToken}` },
                });
                if (!resp.ok) return;
                const pins = await resp.json();
                for (const pin of pins) {
                    this.handleEvent({
                        type: "kb_answer",
                        pinned: true,
                        pin_id: pin.id,
                        chunk_id: pin.chunk_id,
                        doc_id: pin.doc_id,
                        doc_title: pin.doc_title,
                        source_url: pin.source_url,
                        snippet: pin.chunk_text,
                        confidence: 1.0,
                        source: "pin_rehydrate",
                    });
                }
            } catch (err) {
                console.warn("KB pin rehydrate failed", err);
            }
        }

        /** Main entry — call with each kb_answer message from the WebSocket. */
        handleEvent(event) {
            if (!this.container) return;
            if (!event || event.type !== "kb_answer") return;

            const chunkId = event.chunk_id;
            if (!chunkId) return;

            // De-dupe: if we're already showing this chunk, keep it and restart its timer.
            if (this._active.has(chunkId)) {
                this._resetFadeTimer(chunkId);
                return;
            }
            // Pinned chunks never re-surface as fresh cards.
            if (this._pinned.has(chunkId) && !event.pinned) {
                return;
            }

            this._dismissEmptyState();

            const el = makeCard(event, {
                onPin: (node) => this._onPin(event, node),
                onDismiss: (node) => this._dismiss(chunkId, node),
                onExpand: (node) => node.classList.toggle("expanded"),
            });

            if (event.pinned) {
                this._pinned.set(chunkId, {
                    chunk_id: chunkId,
                    doc_id: event.doc_id,
                    pin_id: event.pin_id,
                    element: el,
                });
                this.container.prepend(el);
                return;  // pinned cards never auto-fade
            }

            this.container.prepend(el);
            this._active.set(chunkId, { element: el, timerId: null });
            this._resetFadeTimer(chunkId);

            // Pause fade while the user interacts with the card.
            el.addEventListener("mouseenter", () => this._pauseFade(chunkId));
            el.addEventListener("mouseleave", () => this._resetFadeTimer(chunkId));
            el.addEventListener("click", () => this._resetFadeTimer(chunkId));

            // Cap visible active cards — oldest non-pinned falls into history.
            this._enforceVisibleCap();
        }

        _dismissEmptyState() {
            const empty = this.container.querySelector(".kb-empty");
            if (empty) empty.remove();
        }

        _resetFadeTimer(chunkId) {
            const entry = this._active.get(chunkId);
            if (!entry) return;
            if (entry.timerId) clearTimeout(entry.timerId);
            entry.element.classList.remove("fading");
            entry.timerId = setTimeout(() => {
                entry.element.classList.add("fading");
                setTimeout(() => this._moveToHistory(chunkId), 400);
            }, AUTO_FADE_MS);
        }

        _pauseFade(chunkId) {
            const entry = this._active.get(chunkId);
            if (!entry || !entry.timerId) return;
            clearTimeout(entry.timerId);
            entry.timerId = null;
        }

        _dismiss(chunkId, node) {
            const entry = this._active.get(chunkId);
            if (entry && entry.timerId) clearTimeout(entry.timerId);
            if (this._pinned.has(chunkId)) return;  // dismiss for pinned handled via unpin
            this._active.delete(chunkId);
            node.classList.add("removing");
            setTimeout(() => node.remove(), 200);
        }

        _moveToHistory(chunkId) {
            const entry = this._active.get(chunkId);
            if (!entry) return;
            this._active.delete(chunkId);
            if (entry.timerId) clearTimeout(entry.timerId);
            if (this.historyDrawer) {
                const clone = entry.element.cloneNode(true);
                clone.classList.remove("fading", "removing");
                this.historyDrawer.prepend(clone);
                this._historyCount += 1;
                if (this.historyCount) this.historyCount.textContent = String(this._historyCount);
            }
            entry.element.remove();
        }

        _enforceVisibleCap() {
            const visible = this.container.querySelectorAll(".kb-card:not(.pinned)");
            if (visible.length <= MAX_VISIBLE_CARDS) return;
            const oldest = visible[visible.length - 1];
            const chunkId = oldest.dataset.chunkId;
            if (chunkId) this._moveToHistory(chunkId);
        }

        _toggleHistory() {
            if (!this.historyDrawer || !this.historyToggle) return;
            const isOpen = this.historyToggle.getAttribute("aria-expanded") === "true";
            this.historyToggle.setAttribute("aria-expanded", String(!isOpen));
            this.historyDrawer.hidden = isOpen;
        }

        async _onPin(event, node) {
            if (!this.contactId || !this.apiToken) {
                console.warn("Pin requires contact_id and API token");
                return;
            }
            const chunkId = event.chunk_id;
            const alreadyPinned = this._pinned.has(chunkId);
            try {
                if (alreadyPinned) {
                    const pinId = this._pinned.get(chunkId).pin_id;
                    await fetch(`${API_BASE}/kb/pins/${pinId}`, {
                        method: "DELETE",
                        headers: { Authorization: `Bearer ${this.apiToken}` },
                    });
                    this._pinned.delete(chunkId);
                    node.classList.remove("pinned");
                    node.querySelector(".kb-pin-btn").classList.remove("pinned");
                } else {
                    const resp = await fetch(`${API_BASE}/kb/pins`, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            Authorization: `Bearer ${this.apiToken}`,
                        },
                        body: JSON.stringify({ contact_id: this.contactId, chunk_id: chunkId }),
                    });
                    if (!resp.ok) throw new Error(`Pin failed: ${resp.status}`);
                    const pin = await resp.json();
                    this._pinned.set(chunkId, {
                        chunk_id: chunkId,
                        doc_id: event.doc_id,
                        pin_id: pin.id,
                        element: node,
                    });
                    node.classList.add("pinned");
                    node.querySelector(".kb-pin-btn").classList.add("pinned");
                    // Stop the auto-fade: pinning promotes to permanent.
                    this._pauseFade(chunkId);
                    this._active.delete(chunkId);
                }
            } catch (err) {
                console.error("KB pin toggle failed", err);
            }
        }
    }

    window.KbCardController = KbCardController;

    // Auto-instantiate on DOM ready so the global `window.kbCards` is available
    // for any WebSocket integrator (existing or future). Reads the same
    // localStorage API key as the rest of the demo UI.
    function bootstrap() {
        if (window.kbCards) return;
        const container = document.getElementById("liveKbAnswers");
        if (!container) return;  // live-call view not present
        const token = localStorage.getItem("callsight-api-key");
        if (token) window.__CALLSIGHT_API_TOKEN__ = token;
        window.kbCards = new KbCardController();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
