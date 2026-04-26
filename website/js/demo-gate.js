/*
 * Public demo gate.
 *
 * First-time visitors get 60 seconds of the demo before a modal asks for
 * their email. Submitting the email persists a flag in localStorage so
 * the gate never re-appears for that browser. The CTA on the thank-you
 * state points them at the 14-day sandbox signup.
 */
(function () {
    'use strict';

    var GATE_SECONDS = 60;
    var API_EMAIL_CAPTURE = '/api/v1/demo/email-capture';
    var STORAGE_CAPTURED = 'linda-demo-email-captured';
    var STORAGE_COUNTDOWN = 'linda-demo-gate-deadline';

    function alreadyCaptured() {
        try { return localStorage.getItem(STORAGE_CAPTURED) === '1'; } catch (e) { return false; }
    }

    function markCaptured() {
        try { localStorage.setItem(STORAGE_CAPTURED, '1'); } catch (e) {}
        try { localStorage.removeItem(STORAGE_COUNTDOWN); } catch (e) {}
    }

    function getDeadline() {
        try {
            var raw = localStorage.getItem(STORAGE_COUNTDOWN);
            if (raw) {
                var n = parseInt(raw, 10);
                if (!isNaN(n)) return n;
            }
        } catch (e) {}
        var deadline = Date.now() + GATE_SECONDS * 1000;
        try { localStorage.setItem(STORAGE_COUNTDOWN, String(deadline)); } catch (e) {}
        return deadline;
    }

    function parseUtm() {
        var out = {};
        try {
            var params = new URLSearchParams(window.location.search);
            ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'].forEach(function (k) {
                var v = params.get(k);
                if (v) out[k] = v;
            });
        } catch (e) {}
        return out;
    }

    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        if (attrs) {
            for (var k in attrs) {
                if (k === 'class') node.className = attrs[k];
                else if (k.indexOf('on') === 0) node.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
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

    function renderGate() {
        var overlay = el('div', { class: 'demo-gate', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'demo-gate-title' });
        var card = el('div', { class: 'demo-gate-card' });

        var heading = el('h2', { id: 'demo-gate-title', class: 'demo-gate-title' }, "Want to keep exploring?");
        var copy = el('p', { class: 'demo-gate-copy' },
            "Drop your email and I'll unlock the rest of the demo — no signup required. "
            + "When you're ready for the real thing, start a 14-day sandbox with your own data.");

        var form = el('form', { class: 'demo-gate-form', onSubmit: onSubmit });
        var input = el('input', {
            class: 'demo-gate-input',
            type: 'email',
            name: 'email',
            required: 'true',
            placeholder: 'you@company.com',
            autocomplete: 'email',
            'aria-label': 'Work email',
        });
        var submit = el('button', { class: 'btn-primary demo-gate-submit', type: 'submit' }, 'Unlock the demo');
        var error = el('p', { class: 'demo-gate-error', role: 'alert', 'aria-live': 'polite' });
        form.appendChild(input);
        form.appendChild(submit);
        form.appendChild(error);

        var success = el('div', { class: 'demo-gate-success', hidden: 'true' }, [
            el('h2', { class: 'demo-gate-title' }, "You're in."),
            el('p', { class: 'demo-gate-copy' },
                "The full demo is yours to explore. When you want to run your own calls through Linda, "
                + "start a 14-day sandbox — mock data stays, plus you can upload up to 120 minutes."),
            el('div', { class: 'demo-gate-actions' }, [
                el('a', { class: 'btn-primary', href: (window.lindaAppUrl ? window.lindaAppUrl('/app/signup') : '/app/signup') }, 'Start free sandbox'),
                el('button', { class: 'btn-ghost', type: 'button', onClick: dismiss }, 'Keep exploring the demo'),
            ]),
        ]);

        card.appendChild(heading);
        card.appendChild(copy);
        card.appendChild(form);
        card.appendChild(success);
        overlay.appendChild(card);
        document.body.appendChild(overlay);
        setTimeout(function () { input.focus(); }, 50);

        function onSubmit(ev) {
            ev.preventDefault();
            error.textContent = '';
            submit.disabled = true;
            submit.textContent = 'Sending…';
            fetch(API_EMAIL_CAPTURE, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email: input.value.trim(), source: 'public-demo', utm: parseUtm() }),
            }).then(function (r) {
                if (!r.ok) throw new Error('capture failed (' + r.status + ')');
                markCaptured();
                form.hidden = true;
                success.hidden = false;
            }).catch(function (e) {
                submit.disabled = false;
                submit.textContent = 'Unlock the demo';
                error.textContent = "Couldn't save that — try again in a moment.";
            });
        }

        function dismiss() {
            overlay.classList.add('demo-gate-dismissing');
            setTimeout(function () { overlay.remove(); }, 200);
        }

        return overlay;
    }

    function scheduleGate() {
        if (alreadyCaptured()) return;
        var deadline = getDeadline();
        var msLeft = Math.max(0, deadline - Date.now());
        setTimeout(function () {
            if (!alreadyCaptured()) renderGate();
        }, msLeft);
    }

    function init() {
        // Only mount on the demo shell — never on marketing, never on the SPA.
        if (!document.querySelector('.app-layout')) return;
        // Respect hosts that embed the demo in iframes (embed=1 disables the gate).
        try {
            if (new URLSearchParams(window.location.search).get('embed') === '1') return;
        } catch (e) {}
        scheduleGate();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.lindaDemoGate = { show: renderGate, reset: function () {
        try { localStorage.removeItem(STORAGE_CAPTURED); localStorage.removeItem(STORAGE_COUNTDOWN); } catch (e) {}
    } };
})();
