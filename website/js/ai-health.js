/* CallSight AI-health dashboard widget — Tier 2 + Tier 3 transparency.
 *
 * Renders a small AI health card into any element with id="cs-ai-health-card"
 * by polling /api/v1/analytics/ai-health every 60s.  Also renders a "Pending
 * vocabulary" panel into id="cs-vocab-pending" if present.
 */
(function () {
  'use strict';

  const API_BASE = (window.CALLSIGHT_API_BASE || '/api/v1').replace(/\/$/, '');
  const POLL_INTERVAL_MS = 60_000;

  function _apiKey() {
    return window.CALLSIGHT_API_KEY || localStorage.getItem('callsight_api_key') || '';
  }

  function _fmtPct(value, fractionDigits) {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    return (value * 100).toFixed(fractionDigits == null ? 1 : fractionDigits) + '%';
  }

  function _fmtScore(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    return Number(value).toFixed(2);
  }

  async function _fetchJson(path) {
    const apiKey = _apiKey();
    if (!apiKey) return null;
    const resp = await fetch(API_BASE + path, {
      headers: { 'Authorization': 'Bearer ' + apiKey },
    });
    if (!resp.ok) return null;
    return resp.json();
  }

  async function renderAiHealth() {
    const card = document.getElementById('cs-ai-health-card');
    if (!card) return;
    card.innerHTML = '<div class="cs-ai-health-loading">Loading AI health…</div>';
    const data = await _fetchJson('/analytics/ai-health');
    if (!data) {
      card.innerHTML = '<div class="cs-ai-health-error">Could not load AI health metrics.</div>';
      return;
    }
    card.innerHTML = `
      <div class="cs-ai-health-grid">
        <div class="cs-ai-health-stat">
          <div class="cs-ai-health-label">Quality (7d)</div>
          <div class="cs-ai-health-value">${_fmtScore(data.quality_score_avg_7d)}</div>
          <div class="cs-ai-health-sub">vs 30d ${_fmtScore(data.quality_score_avg_30d)}</div>
        </div>
        <div class="cs-ai-health-stat">
          <div class="cs-ai-health-label">Feedback events (7d)</div>
          <div class="cs-ai-health-value">${data.feedback_events_7d}</div>
        </div>
        <div class="cs-ai-health-stat">
          <div class="cs-ai-health-label">Word error rate (7d)</div>
          <div class="cs-ai-health-value">${_fmtPct(data.asr_wer_7d)}</div>
        </div>
        <div class="cs-ai-health-stat">
          <div class="cs-ai-health-label">Pending vocab</div>
          <div class="cs-ai-health-value">${data.pending_vocab_candidates}</div>
        </div>
        <div class="cs-ai-health-stat">
          <div class="cs-ai-health-label">Flagged for review</div>
          <div class="cs-ai-health-value">${data.flagged_for_review_count}</div>
        </div>
      </div>
    `;
  }

  async function renderVocabPending() {
    const panel = document.getElementById('cs-vocab-pending');
    if (!panel) return;
    panel.innerHTML = '<div class="cs-ai-health-loading">Loading vocabulary candidates…</div>';
    const rows = await _fetchJson('/analytics/vocabulary-pending');
    if (!Array.isArray(rows)) {
      panel.innerHTML = '<div class="cs-ai-health-error">Could not load vocabulary candidates.</div>';
      return;
    }
    if (rows.length === 0) {
      panel.innerHTML = '<div class="cs-ai-health-empty">No pending vocabulary candidates 🎉</div>';
      return;
    }
    const items = rows.map((r) => `
      <li class="cs-vocab-row">
        <span class="cs-vocab-term">${r.term}</span>
        <span class="cs-vocab-meta">${r.confidence} · ${r.source || 'unknown'} · seen ${r.occurrence_count}</span>
        <span class="cs-vocab-actions">
          <button data-action="approve" data-id="${r.id}">Approve</button>
          <button data-action="reject"  data-id="${r.id}">Reject</button>
        </span>
      </li>
    `).join('');
    panel.innerHTML = `<ul class="cs-vocab-list">${items}</ul>`;
    panel.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      btn.disabled = true;
      const path = `/evaluation/vocabulary/${btn.dataset.id}/${btn.dataset.action === 'approve' ? 'approve' : 'reject'}`;
      const apiKey = _apiKey();
      const resp = await fetch(API_BASE + path, {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + apiKey },
      });
      if (resp.ok) {
        btn.closest('.cs-vocab-row').remove();
      } else {
        btn.disabled = false;
      }
    });
  }

  async function refreshAll() {
    await Promise.all([renderAiHealth(), renderVocabPending()]);
  }

  document.addEventListener('DOMContentLoaded', () => {
    refreshAll();
    setInterval(refreshAll, POLL_INTERVAL_MS);
  });

  window.CallSightAiHealth = { refreshAll };
})();
