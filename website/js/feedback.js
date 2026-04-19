/* CallSight feedback hook — batches UI events and POSTs to /api/v1/feedback/batch.
 *
 * Exposes a small global `CallSightFeedback` API:
 *   CallSightFeedback.emit({surface, event_type, ...})
 *   CallSightFeedback.thumbsWidget(container, ctx)   — attach a thumbs up/down to a DOM node
 *   CallSightFeedback.ratingWidget(container, ctx)   — attach a 1–5 star rating
 *   CallSightFeedback.trackReplyEdit(textarea, ctx)  — track edits to a reply textarea
 *   CallSightFeedback.classificationOverride(button, ctx) — fires classification_overridden
 *
 * Events buffer in memory and flush every 5s OR on `pagehide` / `beforeunload`.
 */
(function () {
  'use strict';

  const API_BASE = (window.CALLSIGHT_API_BASE || '/api/v1').replace(/\/$/, '');
  const FLUSH_INTERVAL_MS = 5000;
  const SESSION_ID = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : null;

  let buffer = [];
  let lastFlush = 0;

  function _apiKey() {
    return window.CALLSIGHT_API_KEY || localStorage.getItem('callsight_api_key') || '';
  }

  function _flush(reason) {
    if (buffer.length === 0) return Promise.resolve();
    const events = buffer.splice(0, buffer.length);
    const apiKey = _apiKey();
    if (!apiKey) {
      console.warn('[feedback] no API key; dropping ' + events.length + ' events (' + reason + ')');
      return Promise.resolve();
    }
    const payload = JSON.stringify({ events });
    const url = API_BASE + '/feedback/batch';
    // sendBeacon for unload paths (no awaiting); fetch otherwise.
    if (reason === 'unload' && navigator.sendBeacon) {
      try {
        navigator.sendBeacon(
          url,
          new Blob([payload], { type: 'application/json' })
        );
        return Promise.resolve();
      } catch (e) {
        /* fall through to fetch */
      }
    }
    return fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + apiKey,
      },
      body: payload,
      keepalive: reason === 'unload',
    }).catch((err) => console.warn('[feedback] flush failed:', err));
  }

  function emit(ev) {
    if (!ev || !ev.surface || !ev.event_type) {
      console.warn('[feedback] emit() called with missing surface/event_type');
      return;
    }
    buffer.push({
      surface: ev.surface,
      event_type: ev.event_type,
      signal_type: ev.signal_type || 'explicit',
      interaction_id: ev.interaction_id || null,
      conversation_id: ev.conversation_id || null,
      action_item_id: ev.action_item_id || null,
      user_id: ev.user_id || null,
      insight_dimension: ev.insight_dimension || null,
      payload: ev.payload || {},
      session_id: SESSION_ID,
    });
    if (buffer.length >= 20) {
      _flush('buffer_full');
    }
  }

  function _scheduleFlush() {
    setInterval(() => _flush('interval'), FLUSH_INTERVAL_MS);
    window.addEventListener('pagehide', () => _flush('unload'));
    window.addEventListener('beforeunload', () => _flush('unload'));
  }

  // ── Drop-in widgets ─────────────────────────────────────

  function thumbsWidget(container, ctx) {
    if (!container) return;
    const wrap = document.createElement('div');
    wrap.className = 'cs-feedback-thumbs';
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', 'Was this insight helpful?');
    wrap.innerHTML = `
      <button type="button" class="cs-feedback-thumb cs-feedback-thumb-up" data-vote="up" aria-label="Helpful">👍</button>
      <button type="button" class="cs-feedback-thumb cs-feedback-thumb-down" data-vote="down" aria-label="Not helpful">👎</button>
    `;
    wrap.addEventListener('click', (e) => {
      const btn = e.target.closest('.cs-feedback-thumb');
      if (!btn) return;
      const vote = btn.dataset.vote;
      wrap.querySelectorAll('.cs-feedback-thumb').forEach((b) =>
        b.classList.remove('cs-active')
      );
      btn.classList.add('cs-active');
      emit({
        surface: ctx.surface || 'analysis',
        event_type: 'insight_section_helpful',
        insight_dimension: ctx.dimension || null,
        interaction_id: ctx.interaction_id || null,
        conversation_id: ctx.conversation_id || null,
        action_item_id: ctx.action_item_id || null,
        payload: { vote },
      });
      if (vote === 'down' && !wrap.querySelector('textarea')) {
        const ta = document.createElement('textarea');
        ta.placeholder = 'What was wrong? (optional, max 500 chars)';
        ta.maxLength = 500;
        ta.className = 'cs-feedback-comment';
        ta.addEventListener('blur', () => {
          if (ta.value.trim()) {
            emit({
              surface: ctx.surface || 'analysis',
              event_type: 'insight_rated',
              insight_dimension: ctx.dimension || null,
              interaction_id: ctx.interaction_id || null,
              conversation_id: ctx.conversation_id || null,
              payload: { rating: 1, free_text: ta.value.trim() },
            });
          }
        });
        wrap.appendChild(ta);
      }
    });
    container.appendChild(wrap);
  }

  function ratingWidget(container, ctx) {
    if (!container) return;
    const wrap = document.createElement('div');
    wrap.className = 'cs-feedback-rating';
    wrap.setAttribute('role', 'radiogroup');
    wrap.setAttribute('aria-label', 'Rate this analysis');
    for (let i = 1; i <= 5; i++) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cs-feedback-star';
      btn.dataset.rating = String(i);
      btn.textContent = '★';
      btn.setAttribute('aria-label', `Rate ${i} of 5`);
      wrap.appendChild(btn);
    }
    wrap.addEventListener('click', (e) => {
      const btn = e.target.closest('.cs-feedback-star');
      if (!btn) return;
      const rating = parseInt(btn.dataset.rating, 10);
      wrap.querySelectorAll('.cs-feedback-star').forEach((b, idx) => {
        b.classList.toggle('cs-active', idx < rating);
      });
      emit({
        surface: ctx.surface || 'analysis',
        event_type: 'insight_rated',
        interaction_id: ctx.interaction_id || null,
        conversation_id: ctx.conversation_id || null,
        payload: { rating },
      });
    });
    container.appendChild(wrap);
  }

  function trackReplyEdit(textarea, ctx) {
    if (!textarea) return;
    const initial = textarea.value;
    textarea.dataset.csInitial = initial;
    textarea.addEventListener('blur', () => {
      const updated = textarea.value;
      if (updated === initial) return;
      // The actual reply_edited_before_send / reply_sent_unchanged event is
      // emitted server-side on send; here we only emit a transient indicator
      // that the user touched the draft.
      emit({
        surface: 'email_reply',
        event_type: 'reply_drafted',
        signal_type: 'implicit',
        conversation_id: ctx.conversation_id || null,
        payload: {
          edited_locally: true,
          initial_length: initial.length,
          updated_length: updated.length,
        },
      });
    });
  }

  function classificationOverride(button, ctx) {
    if (!button) return;
    button.addEventListener('click', () => {
      const newClassification = prompt(
        'New classification (sales / support / it / other)?',
        ctx.current || ''
      );
      if (!newClassification) return;
      emit({
        surface: 'email_classifier',
        event_type: 'classification_overridden',
        conversation_id: ctx.conversation_id || null,
        interaction_id: ctx.interaction_id || null,
        payload: {
          old_classification: ctx.current,
          new_classification: newClassification.trim().toLowerCase(),
          confidence: ctx.confidence,
        },
      });
    });
  }

  // ── Auto-attach via data-attributes ─────────────────────
  // Any element with data-cs-thumbs="..." becomes a thumbs widget.
  // Any element with data-cs-rating="..." becomes a 1–5 star rating.

  function autoAttach(root) {
    root = root || document;
    root.querySelectorAll('[data-cs-thumbs]').forEach((el) => {
      if (el.dataset.csInitialized) return;
      el.dataset.csInitialized = '1';
      thumbsWidget(el, {
        surface: el.dataset.csSurface || 'analysis',
        dimension: el.dataset.csDimension || null,
        interaction_id: el.dataset.csInteractionId || null,
        conversation_id: el.dataset.csConversationId || null,
        action_item_id: el.dataset.csActionItemId || null,
      });
    });
    root.querySelectorAll('[data-cs-rating]').forEach((el) => {
      if (el.dataset.csInitialized) return;
      el.dataset.csInitialized = '1';
      ratingWidget(el, {
        surface: el.dataset.csSurface || 'analysis',
        interaction_id: el.dataset.csInteractionId || null,
        conversation_id: el.dataset.csConversationId || null,
      });
    });
  }

  // Public API
  window.CallSightFeedback = {
    emit,
    thumbsWidget,
    ratingWidget,
    trackReplyEdit,
    classificationOverride,
    autoAttach,
    flush: () => _flush('manual'),
  };

  document.addEventListener('DOMContentLoaded', () => {
    _scheduleFlush();
    autoAttach(document);
  });
})();
