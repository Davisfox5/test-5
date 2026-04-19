/*
 * Ask Linda — floating chat widget for the demo shell.
 *
 * - Fetches /api/v1/chat/ping on load; hides itself if the tenant is white-label
 *   (the endpoint returns 404 for white-label tenants).
 * - Streams SSE events from POST /api/v1/chat via fetch + ReadableStream.
 * - Renders write proposals as bordered cards with Confirm / Edit / Cancel.
 * - Persists the conversation id and a lightweight message history to
 *   localStorage so the panel survives page reloads.
 */
(function () {
    'use strict';

    const API_BASE = '/api/v1';
    const STORAGE_CONVO_KEY = 'linda-chat-conversation-id';
    const STORAGE_HISTORY_KEY = 'linda-chat-history';
    const STORAGE_OPEN_KEY = 'linda-chat-open';

    const state = {
        conversationId: localStorage.getItem(STORAGE_CONVO_KEY) || null,
        history: safeParse(localStorage.getItem(STORAGE_HISTORY_KEY), []),
        open: localStorage.getItem(STORAGE_OPEN_KEY) === '1',
        sending: false,
        proposals: new Map(),  // proposal_id -> DOM node
    };

    function safeParse(raw, fallback) {
        try { return raw ? JSON.parse(raw) : fallback; } catch (e) { return fallback; }
    }

    function saveHistory() {
        try {
            localStorage.setItem(STORAGE_HISTORY_KEY, JSON.stringify(state.history.slice(-40)));
        } catch (e) {}
    }

    function saveConversationId() {
        try {
            if (state.conversationId) localStorage.setItem(STORAGE_CONVO_KEY, state.conversationId);
        } catch (e) {}
    }

    function authHeaders() {
        const key = localStorage.getItem('linda-api-key');
        return key ? { 'X-API-Key': key } : {};
    }

    // ── DOM ────────────────────────────────────────────────────────────

    function el(tag, attrs, children) {
        const node = document.createElement(tag);
        if (attrs) {
            for (const k in attrs) {
                if (k === 'class') node.className = attrs[k];
                else if (k === 'dataset') Object.assign(node.dataset, attrs[k]);
                else if (k.startsWith('on')) node.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
                else if (k === 'html') node.innerHTML = attrs[k];
                else node.setAttribute(k, attrs[k]);
            }
        }
        if (children) {
            (Array.isArray(children) ? children : [children]).forEach(function (c) {
                if (c == null) return;
                node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
            });
        }
        return node;
    }

    function buildWidget() {
        const button = el('button', {
            class: 'linda-chat-fab',
            type: 'button',
            'aria-label': 'Ask Linda',
            title: 'Ask Linda',
            onClick: togglePanel,
        }, el('span', { class: 'linda-chat-fab-mark', 'aria-hidden': 'true',
            html: '<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">'
                + '<defs><linearGradient id="fab-grad" x1="0" y1="0" x2="1" y2="1">'
                + '<stop offset="0%" stop-color="#6366F1"/><stop offset="100%" stop-color="#8B5CF6"/></linearGradient></defs>'
                + '<ellipse cx="10" cy="20" rx="6" ry="7" fill="url(#fab-grad)" fill-opacity="0.35"/>'
                + '<ellipse cx="19" cy="15" rx="9" ry="11" fill="url(#fab-grad)"/>'
                + '</svg>',
        }));

        const panel = el('aside', {
            class: 'linda-chat-panel',
            role: 'dialog',
            'aria-label': 'Ask Linda',
            'aria-hidden': 'true',
        }, [
            el('header', { class: 'linda-chat-header' }, [
                el('span', { class: 'linda-chat-title' }, 'Ask Linda'),
                el('div', { class: 'linda-chat-header-actions' }, [
                    el('button', {
                        class: 'linda-chat-new',
                        type: 'button',
                        'aria-label': 'Start a new conversation',
                        title: 'New conversation',
                        onClick: newConversation,
                        html: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>',
                    }),
                    el('button', {
                        class: 'linda-chat-close',
                        type: 'button',
                        'aria-label': 'Close chat',
                        onClick: closePanel,
                        html: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
                    }),
                ]),
            ]),
            el('div', { class: 'linda-chat-log', id: 'linda-chat-log' }),
            el('form', { class: 'linda-chat-composer', onSubmit: onSubmit }, [
                el('textarea', {
                    class: 'linda-chat-input',
                    id: 'linda-chat-input',
                    rows: '2',
                    placeholder: 'Ask Linda anything — "summarize yesterday\'s calls", "follow up with Acme"…',
                    required: 'true',
                    onKeydown: function (e) {
                        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSubmit(e); }
                    },
                }),
                el('button', { class: 'btn-primary linda-chat-send', type: 'submit' }, 'Send'),
            ]),
        ]);

        document.body.appendChild(button);
        document.body.appendChild(panel);

        return { button: button, panel: panel };
    }

    function togglePanel() { state.open ? closePanel() : openPanel(); }

    function openPanel() {
        state.open = true;
        try { localStorage.setItem(STORAGE_OPEN_KEY, '1'); } catch (e) {}
        document.querySelector('.linda-chat-panel').setAttribute('aria-hidden', 'false');
        document.querySelector('.linda-chat-fab').classList.add('is-open');
        renderHistory();
        setTimeout(function () {
            const input = document.getElementById('linda-chat-input');
            if (input) input.focus();
        }, 50);
    }

    function closePanel() {
        state.open = false;
        try { localStorage.setItem(STORAGE_OPEN_KEY, '0'); } catch (e) {}
        document.querySelector('.linda-chat-panel').setAttribute('aria-hidden', 'true');
        document.querySelector('.linda-chat-fab').classList.remove('is-open');
    }

    function newConversation() {
        if (state.sending) return;  // don't drop an in-flight stream
        state.conversationId = null;
        state.history = [];
        state.proposals.clear();
        try {
            localStorage.removeItem(STORAGE_CONVO_KEY);
            localStorage.removeItem(STORAGE_HISTORY_KEY);
        } catch (e) {}
        const log = document.getElementById('linda-chat-log');
        if (log) log.innerHTML = '';
        const input = document.getElementById('linda-chat-input');
        if (input) { input.value = ''; input.focus(); }
    }

    // ── Rendering ──────────────────────────────────────────────────────

    function renderHistory() {
        const log = document.getElementById('linda-chat-log');
        if (!log) return;
        log.innerHTML = '';
        state.history.forEach(function (m) {
            if (m.kind === 'proposal') return appendProposalCard(m.proposal, /*rehydrating*/true);
            appendMessage(m.role, m.text, /*save*/false);
        });
        scrollToBottom();
    }

    function appendMessage(role, text, save) {
        const log = document.getElementById('linda-chat-log');
        if (!log) return null;
        const bubble = el('div', {
            class: 'linda-chat-msg linda-chat-msg-' + role + ' insight-card',
        }, [
            role === 'assistant'
                ? el('span', { class: 'linda-chat-avatar', 'aria-hidden': 'true' }, 'L')
                : null,
            el('p', { class: 'linda-chat-text' }, text),
        ]);
        log.appendChild(bubble);
        scrollToBottom();
        if (save) {
            state.history.push({ role: role, text: text });
            saveHistory();
        }
        return bubble;
    }

    function appendProposalCard(proposal, rehydrating) {
        const log = document.getElementById('linda-chat-log');
        if (!log) return;
        const card = el('div', {
            class: 'linda-proposal-card',
            dataset: { proposalId: proposal.proposal_id, status: proposal.status || 'pending' },
        });
        card.appendChild(el('header', { class: 'linda-proposal-header' }, [
            el('span', { class: 'linda-proposal-kind' }, formatKind(proposal.kind)),
            el('span', { class: 'linda-proposal-status' }, proposal.status || 'pending'),
        ]));
        card.appendChild(renderProposalPreview(proposal));
        const actions = el('div', { class: 'linda-proposal-actions' });
        actions.appendChild(el('button', {
            class: 'btn-primary',
            type: 'button',
            onClick: function () { onConfirmProposal(proposal.proposal_id, card); },
        }, 'Confirm'));
        actions.appendChild(el('button', {
            class: 'btn-ghost',
            type: 'button',
            onClick: function () { onEditProposal(proposal.proposal_id, card, proposal); },
        }, 'Edit'));
        actions.appendChild(el('button', {
            class: 'btn-ghost linda-proposal-cancel',
            type: 'button',
            onClick: function () { onCancelProposal(proposal.proposal_id, card); },
        }, 'Cancel'));
        card.appendChild(actions);
        log.appendChild(card);
        state.proposals.set(proposal.proposal_id, card);
        scrollToBottom();
        if (!rehydrating) {
            state.history.push({ kind: 'proposal', proposal: proposal });
            saveHistory();
        }
    }

    function formatKind(kind) {
        return ({
            action_item: 'Proposed action item',
            email_draft: 'Proposed email draft',
            crm_update: 'Proposed CRM update',
        })[kind] || 'Proposal';
    }

    function renderProposalPreview(proposal) {
        const preview = proposal.preview || {};
        const wrap = el('dl', { class: 'linda-proposal-preview' });
        Object.keys(preview).forEach(function (key) {
            const value = preview[key];
            if (value == null || value === '') return;
            wrap.appendChild(el('dt', null, key.replace(/_/g, ' ')));
            wrap.appendChild(el('dd', null,
                typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
            ));
        });
        return wrap;
    }

    function setProposalStatus(card, status) {
        card.dataset.status = status;
        const statusEl = card.querySelector('.linda-proposal-status');
        if (statusEl) statusEl.textContent = status;
        if (status !== 'pending') {
            card.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
        }
    }

    function scrollToBottom() {
        const log = document.getElementById('linda-chat-log');
        if (log) log.scrollTop = log.scrollHeight;
    }

    // ── Networking ─────────────────────────────────────────────────────

    async function checkAvailable() {
        try {
            const r = await fetch(API_BASE + '/chat/ping', {
                method: 'GET',
                headers: authHeaders(),
            });
            return r.ok;
        } catch (e) {
            return false;
        }
    }

    async function onSubmit(event) {
        if (event && event.preventDefault) event.preventDefault();
        if (state.sending) return;
        const input = document.getElementById('linda-chat-input');
        if (!input) return;
        const text = input.value.trim();
        if (!text) return;

        input.value = '';
        appendMessage('user', text, true);
        const assistantBubble = appendMessage('assistant', '', false);
        const textNode = assistantBubble.querySelector('.linda-chat-text');
        let streamed = '';

        state.sending = true;
        try {
            const body = { message: text };
            if (state.conversationId) body.conversation_id = state.conversationId;
            const resp = await fetch(API_BASE + '/chat', {
                method: 'POST',
                headers: Object.assign(
                    { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
                    authHeaders()
                ),
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                textNode.textContent = '(Linda is unavailable right now — ' + resp.status + ')';
                return;
            }
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            while (true) {
                const chunk = await reader.read();
                if (chunk.done) break;
                buffer += decoder.decode(chunk.value, { stream: true });
                let idx;
                while ((idx = buffer.indexOf('\n\n')) >= 0) {
                    const rawEvent = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    const dataLine = rawEvent.split('\n').find(function (l) { return l.startsWith('data: '); });
                    if (!dataLine) continue;
                    let event;
                    try { event = JSON.parse(dataLine.slice(6)); } catch (e) { continue; }
                    streamed = handleEvent(event, textNode, streamed);
                }
            }
            if (streamed) {
                state.history.push({ role: 'assistant', text: streamed });
                saveHistory();
            }
        } catch (e) {
            textNode.textContent = '(Something went wrong — ' + e.message + ')';
        } finally {
            state.sending = false;
        }
    }

    function handleEvent(event, textNode, streamed) {
        switch (event.type) {
            case 'conversation':
                state.conversationId = event.conversation_id;
                saveConversationId();
                break;
            case 'text':
                streamed += event.delta;
                textNode.textContent = streamed;
                scrollToBottom();
                break;
            case 'proposal':
                appendProposalCard(event.proposal, false);
                break;
            case 'tool_use':
            case 'tool_result':
                // The assistant will typically narrate these; no separate UI needed.
                break;
            case 'error':
                textNode.textContent = (textNode.textContent || '')
                    + ' (stream error: ' + (event.message || 'unknown') + ')';
                break;
            case 'done':
                break;
        }
        return streamed;
    }

    async function onConfirmProposal(id, card) {
        try {
            const resp = await fetch(API_BASE + '/chat/proposals/' + id + '/confirm', {
                method: 'POST',
                headers: authHeaders(),
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const data = await resp.json();
            setProposalStatus(card, data.status || 'confirmed');
        } catch (e) {
            setProposalStatus(card, 'error');
        }
    }

    async function onCancelProposal(id, card) {
        try {
            const resp = await fetch(API_BASE + '/chat/proposals/' + id + '/cancel', {
                method: 'POST',
                headers: authHeaders(),
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const data = await resp.json();
            setProposalStatus(card, data.status || 'cancelled');
        } catch (e) {
            setProposalStatus(card, 'error');
        }
    }

    function onEditProposal(id, card, proposal) {
        if (card.dataset.status !== 'pending') return;
        const preview = card.querySelector('.linda-proposal-preview');
        if (!preview) return;
        // Swap preview for an editable form. Keys match the proposal payload.
        const form = el('form', { class: 'linda-proposal-edit', onSubmit: function (ev) {
            ev.preventDefault();
            const fd = new FormData(form);
            const next = Object.assign({}, proposal.preview || {});
            fd.forEach(function (v, k) { next[k] = v; });
            proposal.preview = next;
            card.replaceChild(renderProposalPreview(proposal), form);
        }});
        Object.keys(proposal.preview || {}).forEach(function (key) {
            const value = (proposal.preview || {})[key];
            const row = el('label', { class: 'linda-proposal-field' });
            row.appendChild(el('span', null, key.replace(/_/g, ' ')));
            const isLong = typeof value === 'string' && value.length > 60;
            const input = el(isLong ? 'textarea' : 'input', {
                name: key,
                value: typeof value === 'object' ? JSON.stringify(value) : (value == null ? '' : String(value)),
            });
            if (!isLong) input.setAttribute('type', 'text');
            row.appendChild(input);
            form.appendChild(row);
        });
        form.appendChild(el('button', { class: 'btn-primary', type: 'submit' }, 'Save edits'));
        card.replaceChild(form, preview);
    }

    // ── Init ───────────────────────────────────────────────────────────

    async function init() {
        // Only mount on the demo shell (where we know there's room for a chat panel).
        if (!document.querySelector('.app-layout')) return;

        const available = await checkAvailable();
        if (!available) return;

        buildWidget();
        if (state.open) openPanel();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.lindaChat = { open: openPanel, close: closePanel, reset: newConversation, state: state };
})();
