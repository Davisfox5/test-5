// ======== MIGRATION: one-time rename of legacy callsight-* localStorage keys ========
(function migrateLegacyStorageKeys() {
    try {
        var legacyPrefix = 'callsight-';
        var newPrefix = 'linda-';
        var legacy = [];
        for (var i = 0; i < localStorage.length; i++) {
            var k = localStorage.key(i);
            if (k && k.indexOf(legacyPrefix) === 0) legacy.push(k);
        }
        legacy.forEach(function(k) {
            var newKey = newPrefix + k.slice(legacyPrefix.length);
            if (localStorage.getItem(newKey) === null) {
                localStorage.setItem(newKey, localStorage.getItem(k));
            }
            localStorage.removeItem(k);
        });
    } catch (e) { /* ignore */ }
})();

// ======== THEME (runs before DOMContentLoaded to avoid flash) ========
(function initTheme() {
    try {
        var stored = localStorage.getItem('linda-theme');
        var prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
        var theme = stored || (prefersLight ? 'light' : 'dark');
        if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
    } catch (e) { /* ignore */ }
})();

// ======== API CONFIGURATION ========
let API_CONNECTED = false;
let API_KEY = localStorage.getItem('linda-api-key') || 'csk__aQLNT3-D21Yiyv60ffeAA9L8XUKVi5HOB58f0c_1wg';

// Channel icon SVG templates for dynamic row building
const CHANNEL_ICONS = {
    voice: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>',
    sms: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    email: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>',
    chat: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
    whatsapp: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>'
};

/**
 * Fetch helper — returns parsed JSON or null when not connected / on error.
 */
async function apiFetch(path, options) {
    if (!API_CONNECTED) return null;
    options = options || {};
    try {
        var resp = await fetch('/api/v1' + path, Object.assign({}, options, {
            headers: Object.assign({ 'Authorization': 'Bearer ' + API_KEY }, options.headers || {})
        }));
        if (!resp.ok) return null;
        return resp.json();
    } catch (e) {
        return null;
    }
}

// ======== HELPERS ========

function sentimentClass(score) {
    if (score >= 7) return 'positive';
    if (score >= 4) return 'neutral';
    return 'negative';
}

function qaBarClass(score) {
    if (score >= 80) return 'high';
    if (score >= 60) return 'mid';
    return 'low';
}

function formatRelativeDate(dateStr) {
    if (!dateStr) return '';
    var d = new Date(dateStr);
    var now = new Date();
    var diffMs = now - d;
    var diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 60) return diffMin + 'm ago';
    var diffH = Math.floor(diffMin / 60);
    if (diffH < 24) return diffH + 'h ago';
    var diffD = Math.floor(diffH / 24);
    if (diffD < 7) return diffD + 'd ago';
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function formatDuration(seconds) {
    if (!seconds) return '--';
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
}

function formatShortDate(dateStr) {
    if (!dateStr) return '--';
    var d = new Date(dateStr);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function escapeHtml(str) {
    if (!str) return '';
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function statusBadgeHtml(status) {
    if (status === 'done' || status === 'complete' || status === 'analyzed') {
        return '<span class="status-badge complete">' + escapeHtml(status.charAt(0).toUpperCase() + status.slice(1)) + '</span>';
    }
    if (status === 'overdue') {
        return '<span class="status-badge" style="background:rgba(244,63,94,0.15);color:var(--accent-rose)">Overdue</span>';
    }
    if (status === 'in_progress') {
        return '<span class="status-badge" style="background:rgba(245,158,11,0.15);color:var(--accent-amber)">In Progress</span>';
    }
    if (status === 'processing') {
        return '<span class="status-badge" style="background:rgba(245,158,11,0.15);color:var(--accent-amber)">Processing</span>';
    }
    return '<span class="status-badge">' + escapeHtml(status.charAt(0).toUpperCase() + status.slice(1)) + '</span>';
}

function priorityBadgeHtml(priority) {
    if (priority === 'high') return '<span class="priority-badge high">High</span>';
    if (priority === 'medium') return '<span class="priority-badge medium">Med</span>';
    if (priority === 'low') return '<span class="priority-badge low">Low</span>';
    return '<span class="priority-badge">' + escapeHtml(priority) + '</span>';
}


// ======== API DATA LOADING FUNCTIONS ========

/**
 * Toggle between loading / empty / error / data states for the interactions panel.
 */
function setInteractionsState(state, errorMessage) {
    var wrap = document.querySelector('#interactions [data-sticky-first]');
    if (!wrap) return;
    var table = wrap.querySelector('table');
    ['loading', 'empty', 'error'].forEach(function(s) {
        var el = wrap.querySelector('[data-state="' + s + '"]');
        if (!el) return;
        if (s === state) {
            el.hidden = false;
            if (s === 'error' && errorMessage) {
                var msg = el.querySelector('.state-error-msg');
                if (msg) msg.textContent = errorMessage;
            }
        } else {
            el.hidden = true;
        }
    });
    if (table) table.style.display = (state === 'data' ? '' : 'none');
}

/**
 * Load interactions from API and rebuild the table tbody.
 */
async function loadInteractions(channel) {
    setInteractionsState('loading');
    var path = '/interactions?limit=50';
    if (channel && channel !== 'all') {
        path += '&channel=' + encodeURIComponent(channel);
    }
    var data;
    try {
        data = await apiFetch(path);
    } catch (e) {
        setInteractionsState('error', 'The server didn\u2019t respond. Check your connection and try again.');
        return;
    }
    if (!data) {
        // Static mode — fall back to existing HTML (mock data)
        setInteractionsState('data');
        return;
    }
    if (!data.length) {
        setInteractionsState('empty');
        return;
    }

    var tbody = document.querySelector('#interactions .interactions-table tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    data.forEach(function(item) {
        var sentiment = 0;
        var qaScore = 0;
        var topics = [];
        var summary = '';
        var actionsDone = 0;
        var actionsTotal = 0;
        var risks = [];

        if (item.insights) {
            sentiment = item.insights.sentiment_score || item.insights.sentiment || 0;
            qaScore = item.insights.qa_score || 0;
            topics = item.insights.topics || [];
            summary = item.insights.summary || '';
            if (item.insights.risk_flags) risks = item.insights.risk_flags;
        }
        if (item.action_items) {
            actionsTotal = item.action_items.length;
            actionsDone = item.action_items.filter(function(a) { return a.status === 'done'; }).length;
        }

        // Unified "Signals" cell: up to 2 topics + up to 1 risk, then +N
        var signals = [];
        topics.slice(0, 2).forEach(function(t) {
            signals.push('<span class="topic-pill">' + escapeHtml(t) + '</span>');
        });
        if (risks.length) {
            var r = risks[0];
            signals.push('<span class="risk-flag ' + escapeHtml(r.type || r) + '" title="' + escapeHtml(r.label || r.type || r) + '">!</span>');
        }
        var hidden = Math.max(0, (topics.length - 2) + Math.max(0, risks.length - 1));
        if (hidden > 0) {
            var tooltip = (topics.slice(2).concat(risks.slice(1).map(function(x) { return x.label || x.type || x; }))).join(', ');
            signals.push('<span class="signals-more" data-tip="' + escapeHtml(tooltip) + '">+' + hidden + '</span>');
        }
        var signalsHtml = '<span class="signals-cell">' + signals.join('') + '</span>';

        var sentVal = (typeof sentiment === 'number') ? sentiment.toFixed(1) : sentiment;
        var sentCls = sentimentClass(parseFloat(sentVal) || 0);
        var qaCls = qaBarClass(qaScore);
        var actionLabel = actionsTotal > 0 ? (actionsDone + '/' + actionsTotal) : '--';
        var actionCls = actionsTotal > 0 ? (actionsDone === actionsTotal ? 'complete' : 'partial') : '';

        var contactName = '';
        if (item.contact && item.contact.name) {
            contactName = item.contact.name;
        } else if (item.participants && item.participants.length > 0) {
            contactName = item.participants.join(', ');
        }

        var duration = item.duration_seconds ? formatDuration(item.duration_seconds) : '--';

        // Main row
        var tr = document.createElement('tr');
        tr.className = 'interaction-row';
        tr.setAttribute('data-channel', item.channel || 'voice');
        tr.setAttribute('data-id', item.id);
        tr.innerHTML =
            '<td><svg class="expand-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><polyline points="9 18 15 12 9 6"/></svg></td>' +
            '<td><span class="channel-icon-svg ' + escapeHtml(item.channel || 'voice') + '" aria-label="' + escapeHtml(item.channel || 'voice') + '">' + (CHANNEL_ICONS[item.channel] || CHANNEL_ICONS.voice) + '</span></td>' +
            '<td class="fw-500 interaction-title col-sticky" data-target="interaction-detail" data-interaction-id="' + item.id + '">' + escapeHtml(item.title || 'Untitled') + '</td>' +
            '<td>' + escapeHtml(contactName) + '</td>' +
            '<td>' + signalsHtml + '</td>' +
            '<td><span class="sentiment-badge ' + sentCls + '">' + sentVal + '</span></td>' +
            '<td><div class="qa-score-bar"><span>' + qaScore + '</span><div class="qa-bar-track"><div class="qa-bar-fill ' + qaCls + '" style="width:' + qaScore + '%"></div></div></div></td>' +
            '<td><span class="action-completion ' + actionCls + '">' + actionLabel + '</span></td>' +
            '<td>' + duration + '</td>' +
            '<td><span class="date-relative" title="' + escapeHtml(item.created_at || '') + '">' + formatRelativeDate(item.created_at) + '</span></td>' +
            '<td>' + statusBadgeHtml(item.status || 'processing') + '</td>';
        tbody.appendChild(tr);

        // Expand row
        var expandTr = document.createElement('tr');
        expandTr.className = 'row-expand';
        expandTr.setAttribute('data-channel', item.channel || 'voice');
        expandTr.innerHTML =
            '<td colspan="11"><div class="row-expand-content">' +
            '<p class="row-expand-summary">' + escapeHtml(summary) + '</p>' +
            '<div class="row-expand-footer">' +
            '<span style="font-size:13px;color:var(--text-muted)">QA: <strong>' + qaScore + '/100</strong></span>' +
            '<button class="btn btn-primary btn-sm" onclick="loadInteractionDetail(\'' + item.id + '\')">View Full Detail &rarr;</button>' +
            '</div></div></td>';
        tbody.appendChild(expandTr);
    });

    setInteractionsState('data');
    // Re-bind row expand/collapse for new rows
    bindRowExpand();
}

/**
 * Load a single interaction detail from API.
 */
async function loadInteractionDetail(interactionId) {
    switchView('interaction-detail');

    var data = await apiFetch('/interactions/' + interactionId);
    if (!data) return;

    // Title
    var titleEl = document.querySelector('#interaction-detail .view-header h1');
    if (titleEl) titleEl.textContent = data.title || 'Interaction Detail';

    // AI Summary
    var summaryEl = document.querySelector('#interaction-detail .insight-card p');
    if (summaryEl && data.insights && data.insights.summary) {
        summaryEl.textContent = data.insights.summary;
    }

    // Coaching notes
    if (data.insights && data.insights.coaching) {
        var coachingContainer = document.querySelector('#interaction-detail .coaching-notes');
        if (coachingContainer) {
            coachingContainer.innerHTML = '';
            var notes = Array.isArray(data.insights.coaching) ? data.insights.coaching : [data.insights.coaching];
            notes.forEach(function(note) {
                var div = document.createElement('div');
                if (typeof note === 'object' && note.type) {
                    div.className = 'notepoint ' + note.type;
                    div.textContent = note.text || note.note || '';
                } else {
                    div.className = 'notepoint positive';
                    div.textContent = String(note);
                }
                coachingContainer.appendChild(div);
            });
        }
    }

    // Transcript segments
    if (data.transcript && data.transcript.length > 0) {
        var scrollEl = document.querySelector('#interaction-detail .transcript-scroll');
        if (scrollEl) {
            scrollEl.innerHTML = '';
            data.transcript.forEach(function(seg) {
                var entry = document.createElement('div');
                entry.className = 'transcript-entry';
                var timeStr = '';
                if (seg.start_time !== undefined) {
                    var m = Math.floor(seg.start_time / 60);
                    var s = Math.floor(seg.start_time % 60);
                    timeStr = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
                } else if (seg.time) {
                    timeStr = seg.time;
                }
                var speakerClass = (seg.role === 'agent' || seg.speaker_role === 'agent') ? 'agent' : 'customer';
                var speakerName = seg.speaker || seg.speaker_name || (speakerClass === 'agent' ? 'Agent' : 'Customer');
                entry.innerHTML =
                    '<span class="entry-time">' + escapeHtml(timeStr) + '</span>' +
                    '<span class="entry-speaker ' + speakerClass + '">' + escapeHtml(speakerName) + '</span>' +
                    '<p class="entry-text">' + escapeHtml(seg.text || seg.content || '') + '</p>';
                scrollEl.appendChild(entry);
            });
        }
    }

    // Call metrics
    if (data.call_metrics) {
        var metricsContainer = document.querySelector('#interaction-detail .call-metrics');
        if (metricsContainer) {
            // Update if there are specific metric elements
        }
    }

    // Duration
    var timeEl = document.querySelector('#interaction-detail .time');
    if (timeEl && data.duration_seconds) {
        timeEl.textContent = '00:00 / ' + formatDuration(data.duration_seconds);
    }
}

/**
 * Load action items from API.
 */
async function loadActionItems(statusFilter) {
    var path = '/action-items';
    var params = [];
    if (statusFilter && statusFilter !== 'all') {
        params.push('status=' + encodeURIComponent(statusFilter));
    }
    if (params.length > 0) path += '?' + params.join('&');

    var data = await apiFetch(path);
    if (!data) return;

    var tbody = document.querySelector('#action-items .interactions-table tbody');
    if (!tbody) return;

    tbody.innerHTML = '';

    data.forEach(function(item) {
        var tr = document.createElement('tr');
        tr.setAttribute('data-status', item.status || 'pending');

        var checked = (item.status === 'done') ? ' checked disabled' : '';
        var dueDateClass = (item.status === 'overdue') ? ' class="due-date overdue"' : '';

        tr.innerHTML =
            '<td><input type="checkbox"' + checked + '></td>' +
            '<td class="fw-500">' + escapeHtml(item.title) + '</td>' +
            '<td>--</td>' +
            '<td><span class="category-pill">' + escapeHtml(item.category || '--') + '</span></td>' +
            '<td>' + priorityBadgeHtml(item.priority || 'medium') + '</td>' +
            '<td>' + escapeHtml(item.assigned_to || '--') + '</td>' +
            '<td>' + statusBadgeHtml(item.status || 'pending') + '</td>' +
            '<td' + dueDateClass + '>' + formatShortDate(item.due_date) + '</td>';

        tbody.appendChild(tr);
    });

    // Update filter counts
    updateActionFilterCounts(data);
}

function updateActionFilterCounts(data) {
    if (!data) return;
    var counts = { all: data.length, pending: 0, in_progress: 0, done: 0, overdue: 0 };
    data.forEach(function(item) {
        var s = item.status || 'pending';
        if (counts[s] !== undefined) counts[s]++;
    });
    document.querySelectorAll('.action-filter-btn').forEach(function(btn) {
        var status = btn.getAttribute('data-status');
        if (status && counts[status] !== undefined) {
            btn.textContent = status.charAt(0).toUpperCase() + status.slice(1).replace('_', ' ') + ' (' + counts[status] + ')';
            if (status === 'all') btn.textContent = 'All (' + counts.all + ')';
        }
    });
}

/**
 * Load contacts from API.
 */
async function loadContacts() {
    var data = await apiFetch('/contacts');
    if (!data) return;

    var tbody = document.querySelector('#contacts .data-table tbody');
    if (!tbody) return;

    tbody.innerHTML = '';

    data.forEach(function(contact) {
        var tr = document.createElement('tr');
        tr.className = 'clickable-row';
        tr.setAttribute('data-target', 'contact-detail');

        var lastSeen = contact.last_seen_at
            ? new Date(contact.last_seen_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
            : '--';

        // Simple sparkline placeholder
        var sparkline = '<svg viewBox="0 0 80 20" class="sparkline-svg inline-sparkline"><polyline points="0,10 20,10 40,10 60,10 80,10" fill="none" stroke="#94A3B8" stroke-width="2"/></svg>';
        if (contact.sentiment_trend && contact.sentiment_trend.length >= 2) {
            var points = contact.sentiment_trend.map(function(v, i) {
                var x = (i / (contact.sentiment_trend.length - 1)) * 80;
                var y = 20 - (v / 10) * 20;
                return x + ',' + y;
            }).join(' ');
            var avg = contact.sentiment_trend.reduce(function(a, b) { return a + b; }, 0) / contact.sentiment_trend.length;
            var color = avg >= 7 ? '#10B981' : (avg >= 4 ? '#94A3B8' : '#F43F5E');
            sparkline = '<svg viewBox="0 0 80 20" class="sparkline-svg inline-sparkline"><polyline points="' + points + '" fill="none" stroke="' + color + '" stroke-width="2"/></svg>';
        }

        tr.innerHTML =
            '<td class="fw-500">' + escapeHtml(contact.name || '--') + '</td>' +
            '<td>' + escapeHtml(contact.company_id ? 'Company' : '--') + '</td>' +
            '<td>' + escapeHtml(contact.phone || '--') + '</td>' +
            '<td>' + escapeHtml(contact.email || '--') + '</td>' +
            '<td>' + (contact.interaction_count || 0) + '</td>' +
            '<td>' + lastSeen + '</td>' +
            '<td>' + sparkline + '</td>';

        tbody.appendChild(tr);
    });
}

/**
 * Load analytics from API.  Fans out across six endpoints and populates
 * each analytics subsection.  Each section is guarded with a
 * ``[data-analytics=...]`` attribute; if the container is absent or the
 * endpoint returns nothing, the existing mock HTML stays untouched.
 */
async function loadAnalytics() {
    var business = await apiFetch('/analytics/business');
    if (business) {
        var gaugeValue = document.querySelector('#analytics .gauge-value');
        if (gaugeValue && business.health_score !== undefined) {
            gaugeValue.textContent = Math.round(business.health_score);
        }
    }

    var dashboard = await apiFetch('/analytics/dashboard');
    if (dashboard) updateDashboardCards(dashboard);

    var trendData = await apiFetch('/analytics/trends?period=7d');
    if (trendData) renderVolumeSentimentChart(trendData);

    var topics = await apiFetch('/analytics/topics?period=30d');
    if (topics) renderTopicsList(topics);

    var competitive = await apiFetch('/analytics/competitive?period=30d');
    if (competitive) renderCompetitorTable(competitive);

    var feedback = await apiFetch('/analytics/product-feedback?period=30d');
    if (feedback) renderFeedbackThemes(feedback);

    var team = await apiFetch('/analytics/team');
    if (team) renderAgentLeaderboard(team);
}

/**
 * Top-of-dashboard stat cards — total interactions, sentiment, action items,
 * QA score — with trend arrows from ``prev_period_deltas``.
 */
function updateDashboardCards(data) {
    var container = document.querySelector('[data-analytics="stat-cards"]');
    if (!container) return;

    var deltas = data.prev_period_deltas || {};
    var cards = [
        {
            label: 'Total Interactions',
            value: (data.total_interactions || 0).toLocaleString(),
            delta: deltas.total_interactions_pct,
            suffix: '%'
        },
        {
            label: 'Avg Sentiment',
            value: data.avg_sentiment_score != null ? data.avg_sentiment_score.toFixed(1) + '/10' : '—',
            delta: deltas.avg_sentiment_pct,
            suffix: '%'
        },
        {
            label: 'Open Action Items',
            value: (data.action_items_open || 0).toLocaleString(),
            delta: null,
            suffix: ''
        },
        {
            label: 'Avg QA Score',
            value: data.avg_qa_score != null ? Math.round(data.avg_qa_score) + '%' : '—',
            delta: deltas.avg_qa_pct,
            suffix: '%'
        }
    ];

    container.innerHTML = cards.map(function(c) {
        var arrow = '';
        if (c.delta != null) {
            var up = c.delta >= 0;
            arrow = '<span class="stat-trend ' + (up ? 'up' : 'down') + '">' +
                (up ? '↑ ' : '↓ ') + Math.abs(c.delta).toFixed(1) + c.suffix + '</span>';
        }
        return '<div class="stat-card">' +
            '<div class="stat-label">' + escapeHtml(c.label) + '</div>' +
            '<div class="stat-value">' + escapeHtml(c.value) + '</div>' +
            arrow +
        '</div>';
    }).join('');
}

/**
 * Dual-axis volume + sentiment chart from /analytics/trends?period=7d.
 * Collapses per-channel rows into per-date totals.
 */
function renderVolumeSentimentChart(points) {
    var container = document.querySelector('[data-analytics="trend-chart"]');
    if (!container || !Array.isArray(points) || !points.length) return;

    // Aggregate over channels → one entry per date.
    var byDate = {};
    points.forEach(function(p) {
        var d = byDate[p.date] || { date: p.date, count: 0, sentSum: 0, sentN: 0 };
        d.count += p.interaction_count || 0;
        if (p.avg_sentiment != null) {
            d.sentSum += p.avg_sentiment * (p.interaction_count || 1);
            d.sentN += (p.interaction_count || 1);
        }
        byDate[p.date] = d;
    });
    var dates = Object.keys(byDate).sort();
    var maxCount = Math.max.apply(null, dates.map(function(d) { return byDate[d].count; })) || 1;

    container.innerHTML = dates.map(function(d) {
        var entry = byDate[d];
        var barPct = Math.round((entry.count / maxCount) * 100);
        var sentiment = entry.sentN ? (entry.sentSum / entry.sentN) : null;
        var dotPct = sentiment != null ? Math.round((sentiment / 10) * 100) : 0;
        var label = new Date(d).toLocaleDateString('en-US', { weekday: 'short' });
        return '<div class="trend-col">' +
            '<div class="trend-bar" style="height:' + barPct + '%"></div>' +
            (sentiment != null
                ? '<div class="trend-dot" style="bottom:' + dotPct + '%"></div>'
                : '') +
            '<div class="trend-label">' + escapeHtml(label) + '</div>' +
        '</div>';
    }).join('');
}

function renderTopicsList(topics) {
    var container = document.querySelector('[data-analytics="topics"]');
    if (!container || !Array.isArray(topics) || !topics.length) return;
    var max = Math.max.apply(null, topics.map(function(t) { return t.mentions || 0; })) || 1;
    container.innerHTML = topics.map(function(t) {
        var pct = Math.round(((t.mentions || 0) / max) * 100);
        var change = '';
        if (t.pct_change != null) {
            var cls = t.pct_change >= 0 ? 'up' : 'down';
            change = '<span class="topic-change ' + cls + '">' +
                (t.pct_change >= 0 ? '+' : '') + t.pct_change.toFixed(1) + '%</span>';
        }
        return '<div class="topic-row">' +
            '<div class="topic-name">' + escapeHtml(t.name) + '</div>' +
            '<div class="topic-bar"><div class="topic-bar-fill" style="width:' + pct + '%"></div></div>' +
            '<div class="topic-count">' + (t.mentions || 0) + '</div>' +
            change +
        '</div>';
    }).join('');
}

function renderCompetitorTable(rows) {
    var container = document.querySelector('[data-analytics="competitors"]');
    if (!container || !Array.isArray(rows) || !rows.length) return;
    container.innerHTML = '<table class="competitor-table"><thead><tr>' +
        '<th>Competitor</th><th>Mentions</th><th>Handled Well</th></tr></thead><tbody>' +
        rows.map(function(r) {
            return '<tr>' +
                '<td>' + escapeHtml(r.competitor) + '</td>' +
                '<td>' + (r.mentions || 0) + '</td>' +
                '<td>' + (r.handled_well_pct != null ? r.handled_well_pct.toFixed(0) + '%' : '—') + '</td>' +
            '</tr>';
        }).join('') +
        '</tbody></table>';
}

function renderFeedbackThemes(themes) {
    var container = document.querySelector('[data-analytics="feedback"]');
    if (!container || !Array.isArray(themes) || !themes.length) return;
    container.innerHTML = themes.map(function(t) {
        var net = (t.positive_count || 0) - (t.negative_count || 0);
        var sign = net >= 0 ? '+' : '−';
        var signCls = net >= 0 ? 'positive' : 'negative';
        var total = (t.positive_count || 0) + (t.negative_count || 0) + (t.neutral_count || 0);
        return '<div class="feedback-row ' + signCls + '">' +
            '<span class="feedback-sign">' + sign + '</span>' +
            '<div class="feedback-theme">' + escapeHtml(t.theme) +
                (t.sample_quote ? '<div class="feedback-quote">' + escapeHtml(t.sample_quote) + '</div>' : '') +
            '</div>' +
            '<span class="feedback-count">' + total + ' mentions</span>' +
        '</div>';
    }).join('');
}

function renderAgentLeaderboard(agents) {
    var container = document.querySelector('[data-analytics="leaderboard"]');
    if (!container || !Array.isArray(agents) || !agents.length) return;
    container.innerHTML = '<table class="leaderboard-table"><thead><tr>' +
        '<th>#</th><th>Agent</th><th>Calls</th><th>Avg Sentiment</th><th>QA Score</th><th>Churn Flags</th>' +
        '</tr></thead><tbody>' +
        agents.map(function(a, i) {
            return '<tr>' +
                '<td>' + (i + 1) + '</td>' +
                '<td>' + escapeHtml(a.name || '—') + '</td>' +
                '<td>' + (a.interaction_count || 0) + '</td>' +
                '<td>' + (a.avg_sentiment != null ? a.avg_sentiment.toFixed(1) : '—') + '</td>' +
                '<td>' + (a.avg_scorecard_score != null ? Math.round(a.avg_scorecard_score) + '%' : '—') + '</td>' +
                '<td>' + (a.churn_flags || 0) + '</td>' +
            '</tr>';
        }).join('') +
        '</tbody></table>';
}

/**
 * Load library snippets from API.
 */
async function loadLibrary() {
    var data = await apiFetch('/library');
    if (!data) return;

    var grid = document.querySelector('#call-library .library-grid');
    if (!grid) return;

    grid.innerHTML = '';

    data.forEach(function(snippet) {
        var qualityClass = (snippet.quality === 'exemplary') ? 'exemplary' : 'needs-improvement';
        var qualityLabel = snippet.quality
            ? snippet.quality.charAt(0).toUpperCase() + snippet.quality.slice(1).replace('_', ' ')
            : 'Unrated';

        var tags = (snippet.tags || []).map(function(t) {
            return '<span class="tag">' + escapeHtml(t) + '</span>';
        }).join('');

        var startMin = Math.floor((snippet.start_time || 0) / 60);
        var startSec = Math.floor((snippet.start_time || 0) % 60);
        var endMin = Math.floor((snippet.end_time || 0) / 60);
        var endSec = Math.floor((snippet.end_time || 0) % 60);
        var timeRange = startMin + ':' + (startSec < 10 ? '0' : '') + startSec +
            ' - ' + endMin + ':' + (endSec < 10 ? '0' : '') + endSec;

        var excerpt = '';
        if (snippet.transcript_excerpt && snippet.transcript_excerpt.length > 0) {
            var first = snippet.transcript_excerpt[0];
            excerpt = (typeof first === 'string') ? first : (first.text || first.content || '');
        }

        var card = document.createElement('div');
        card.className = 'library-card';
        card.innerHTML =
            '<div class="library-card-header">' +
            '<span class="quality-badge ' + qualityClass + '">' + escapeHtml(qualityLabel) + '</span>' +
            '<span class="library-play-btn">\u25B6</span>' +
            '</div>' +
            '<h4 class="library-card-title">' + escapeHtml(snippet.title || 'Untitled Snippet') + '</h4>' +
            '<div class="library-time-range">' + timeRange + '</div>' +
            '<div class="library-source">' + escapeHtml(snippet.description || '') + '</div>' +
            '<p class="library-excerpt">' + escapeHtml(excerpt) + '</p>' +
            '<div class="library-tags">' + tags + '</div>';

        grid.appendChild(card);
    });
}


// ======== ROW EXPAND BINDING (reusable) ========

function bindRowExpand() {
    document.querySelectorAll('.interaction-row').forEach(function(row) {
        // Remove old listeners by cloning
        if (row.getAttribute('data-bound') === '1') return;
        row.setAttribute('data-bound', '1');

        row.addEventListener('click', function(e) {
            if (e.target.closest('.interaction-title')) return;

            var expandRow = row.nextElementSibling;
            if (!expandRow || !expandRow.classList.contains('row-expand')) return;

            document.querySelectorAll('.interaction-row.expanded').forEach(function(r) {
                if (r !== row) {
                    r.classList.remove('expanded');
                    var other = r.nextElementSibling;
                    if (other) other.classList.remove('open');
                }
            });

            row.classList.toggle('expanded');
            expandRow.classList.toggle('open');
        });
    });
}


document.addEventListener('DOMContentLoaded', function() {

    // ======== API CONNECTION CHECK ========
    // Try to detect if we are served by the FastAPI backend
    (async function() {
        try {
            var resp = await fetch('/api/v1/health');
            if (resp.ok) {
                API_CONNECTED = true;
                // If no API key in localStorage, check for a hidden input
                if (!API_KEY) {
                    var input = document.getElementById('apiKey');
                    if (input) API_KEY = input.value;
                }
                // Load initial view data
                loadInteractions();
                // Populate dashboard stat cards (shared across interactions view)
                apiFetch('/analytics/dashboard').then(function(d) {
                    if (d) updateDashboardCards(d);
                });
            }
        } catch (e) {
            // Not connected — static mode, mock HTML stays
        }
    })();

    // ======== VIEW SWITCHING ========
    var navItems = document.querySelectorAll('.nav-item');
    var sections = document.querySelectorAll('.view');
    var viewContainer = document.getElementById('viewContainer');

    window.switchView = function(viewId) {
        sections.forEach(function(s) { s.classList.remove('active'); });
        navItems.forEach(function(n) { n.classList.remove('active'); });

        var targetSection = document.getElementById(viewId);
        var targetNav = document.querySelector('.nav-item[data-view="' + viewId + '"]');

        if (targetSection) targetSection.classList.add('active');
        if (targetNav) targetNav.classList.add('active');

        if (viewContainer) viewContainer.scrollTop = 0;

        // Load API data when switching views (connected mode)
        if (API_CONNECTED) {
            if (viewId === 'interactions') loadInteractions();
            else if (viewId === 'action-items') loadActionItems();
            else if (viewId === 'contacts') loadContacts();
            else if (viewId === 'analytics') loadAnalytics();
            else if (viewId === 'call-library') loadLibrary();
        }
    };

    // Make loadInteractionDetail globally accessible
    window.loadInteractionDetail = loadInteractionDetail;

    navItems.forEach(function(item) {
        item.addEventListener('click', function(e) {
            e.preventDefault();
            var viewId = item.getAttribute('data-view');
            if (viewId) switchView(viewId);
        });
    });

    // Back links
    document.querySelectorAll('.back-link').forEach(function(link) {
        link.addEventListener('click', function(e) {
            e.preventDefault();
            switchView(link.getAttribute('data-back') || 'interactions');
        });
    });

    // Clickable titles -> detail views
    document.addEventListener('click', function(e) {
        var title = e.target.closest('.interaction-title');
        if (title) {
            e.preventDefault();
            var interactionId = title.getAttribute('data-interaction-id');
            if (API_CONNECTED && interactionId) {
                loadInteractionDetail(interactionId);
            } else {
                var target = title.getAttribute('data-target');
                if (target) switchView(target);
            }
        }
    });

    // ======== NAV GROUP COLLAPSE/EXPAND ========
    var navGroups = document.querySelectorAll('.nav-group');
    var savedGroups = JSON.parse(localStorage.getItem('linda-nav-groups') || '{}');

    navGroups.forEach(function(group) {
        var key = group.getAttribute('data-group');
        var header = group.querySelector('.nav-group-header');

        // Restore saved state (default: expanded)
        if (savedGroups[key] === 'collapsed') {
            group.classList.add('collapsed');
        }

        if (header) {
            header.addEventListener('click', function() {
                group.classList.toggle('collapsed');
                // Save state
                var states = {};
                navGroups.forEach(function(g) {
                    states[g.getAttribute('data-group')] = g.classList.contains('collapsed') ? 'collapsed' : 'expanded';
                });
                localStorage.setItem('linda-nav-groups', JSON.stringify(states));
            });
        }
    });

    // ======== ROLE TOGGLE ========
    var roleToggle = document.getElementById('roleToggle');
    var appLayout = document.querySelector('.app-layout');
    var userRole = document.querySelector('.user-role');

    // Restore saved role
    if (localStorage.getItem('linda-role') === 'manager') {
        appLayout.classList.add('manager-mode');
        if (roleToggle) roleToggle.querySelector('[data-role="manager"]').classList.add('active');
        if (roleToggle) roleToggle.querySelector('[data-role="agent"]').classList.remove('active');
        if (userRole) userRole.textContent = 'Manager';
    }

    if (roleToggle) {
        roleToggle.querySelectorAll('.role-btn').forEach(function(btn) {
            btn.addEventListener('click', function() {
                roleToggle.querySelectorAll('.role-btn').forEach(function(b) { b.classList.remove('active'); });
                btn.classList.add('active');
                var role = btn.getAttribute('data-role');
                if (role === 'manager') {
                    appLayout.classList.add('manager-mode');
                    if (userRole) userRole.textContent = 'Manager';
                } else {
                    appLayout.classList.remove('manager-mode');
                    if (userRole) userRole.textContent = 'Sales Agent';
                }
                localStorage.setItem('linda-role', role);
            });
        });
    }

    // ======== CHANNEL FILTER TABS ========
    document.querySelectorAll('.channel-tab').forEach(function(tab) {
        tab.addEventListener('click', function() {
            document.querySelectorAll('.channel-tab').forEach(function(t) { t.classList.remove('active'); });
            tab.classList.add('active');
            var channel = tab.getAttribute('data-channel');

            if (API_CONNECTED) {
                // Fetch filtered data from API
                loadInteractions(channel);
            } else {
                // Static mode — filter existing rows
                document.querySelectorAll('.interaction-row').forEach(function(row) {
                    var expandRow = row.nextElementSibling;
                    if (channel === 'all') {
                        row.style.display = '';
                        if (expandRow && expandRow.classList.contains('row-expand')) {
                            // Keep expand state but don't hide
                        }
                    } else {
                        var match = row.getAttribute('data-channel') === channel;
                        row.style.display = match ? '' : 'none';
                        if (expandRow && expandRow.classList.contains('row-expand')) {
                            expandRow.style.display = match && expandRow.classList.contains('open') ? '' : 'none';
                        }
                    }
                });
            }
        });
    });

    // ======== ROW EXPAND/COLLAPSE ========
    bindRowExpand();

    // ======== ACTION ITEMS FILTER ========
    document.querySelectorAll('.action-filter-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.action-filter-btn').forEach(function(b) { b.classList.remove('active'); });
            btn.classList.add('active');
            var status = btn.getAttribute('data-status');

            if (API_CONNECTED) {
                loadActionItems(status);
            } else {
                // Static mode — filter existing rows
                var actionItems = document.querySelectorAll('#action-items tbody tr');
                actionItems.forEach(function(row) {
                    if (status === 'all') {
                        row.style.display = '';
                    } else {
                        row.style.display = row.getAttribute('data-status') === status ? '' : 'none';
                    }
                });
            }
        });
    });

    // ======== MOCK TRANSCRIPT PLAYBACK ========
    var playBtn = document.querySelector('#interaction-detail .btn-play');
    var progressBar = document.querySelector('#interaction-detail .progress');
    var waves = document.querySelectorAll('#interaction-detail .wave');
    var isPlaying = false;
    var progress = 35;

    if (playBtn) {
        playBtn.addEventListener('click', function() {
            isPlaying = !isPlaying;
            playBtn.innerText = isPlaying ? '\u23F8' : '\u25B6';
            if (isPlaying) simulatePlayback();
        });
    }

    function simulatePlayback() {
        if (!isPlaying) return;
        progress += 0.1;
        if (progress > 100) progress = 0;
        if (progressBar) progressBar.style.width = progress + '%';

        var activeWaveIdx = Math.floor(Math.random() * waves.length);
        waves.forEach(function(w, i) { w.classList.toggle('active', i === activeWaveIdx); });

        requestAnimationFrame(simulatePlayback);
    }

    // (Modal + header-search wiring moved below with full keyboard support)

    // ======== MINI CHARTS (SVG sparklines) ========
    renderMiniCharts();

    // ======== THEME TOGGLE ========
    var themeToggle = document.getElementById('themeToggle');
    if (themeToggle) {
        themeToggle.addEventListener('click', function() {
            var isLight = document.documentElement.getAttribute('data-theme') === 'light';
            var next = isLight ? 'dark' : 'light';
            if (next === 'light') {
                document.documentElement.setAttribute('data-theme', 'light');
            } else {
                document.documentElement.removeAttribute('data-theme');
            }
            try { localStorage.setItem('linda-theme', next); } catch (e) {}
        });
    }

    // ======== SIDEBAR COLLAPSE ========
    var sidebar = document.getElementById('appSidebar');
    var sidebarBtn = document.getElementById('sidebarCollapseBtn');
    if (sidebar) {
        // Restore saved state
        try {
            if (localStorage.getItem('linda-sidebar') === 'collapsed') {
                sidebar.classList.add('collapsed');
                if (sidebarBtn) {
                    sidebarBtn.setAttribute('aria-pressed', 'true');
                    sidebarBtn.setAttribute('aria-label', 'Expand sidebar');
                }
                addCollapsedAriaLabels();
            }
        } catch (e) {}
    }
    if (sidebarBtn) {
        sidebarBtn.addEventListener('click', function() {
            var collapsed = sidebar.classList.toggle('collapsed');
            sidebarBtn.setAttribute('aria-pressed', String(collapsed));
            sidebarBtn.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
            if (collapsed) addCollapsedAriaLabels();
            try { localStorage.setItem('linda-sidebar', collapsed ? 'collapsed' : 'expanded'); } catch (e) {}
        });
    }

    function addCollapsedAriaLabels() {
        // Copy visible text to aria-label so tooltip and screen readers work when collapsed
        document.querySelectorAll('.nav-item').forEach(function(item) {
            if (!item.getAttribute('aria-label')) {
                var label = (item.textContent || '').trim();
                if (label) item.setAttribute('aria-label', label);
            }
        });
    }
    // Always add aria-labels so they're available for collapsed-state tooltip
    addCollapsedAriaLabels();

    // ======== MODAL MANAGEMENT (focus trap + ESC + focus restore) ========
    var activeModal = null;
    var lastFocusedBeforeModal = null;

    function getFocusable(container) {
        return container.querySelectorAll(
            'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
    }
    window.openModal = function(modalEl) {
        if (!modalEl) return;
        lastFocusedBeforeModal = document.activeElement;
        modalEl.classList.add('active');
        modalEl.setAttribute('aria-hidden', 'false');
        activeModal = modalEl;
        document.body.style.overflow = 'hidden';
        // Focus first focusable (prefer input)
        var focusables = getFocusable(modalEl);
        var firstInput = modalEl.querySelector('input, textarea, select');
        if (firstInput) firstInput.focus();
        else if (focusables.length) focusables[0].focus();
    };
    window.closeModal = function(modalEl) {
        if (!modalEl) return;
        modalEl.classList.remove('active');
        modalEl.setAttribute('aria-hidden', 'true');
        if (activeModal === modalEl) activeModal = null;
        document.body.style.overflow = '';
        if (lastFocusedBeforeModal && document.contains(lastFocusedBeforeModal)) {
            lastFocusedBeforeModal.focus();
        }
    };

    document.addEventListener('keydown', function(e) {
        if (!activeModal) return;
        if (e.key === 'Escape') {
            e.preventDefault();
            window.closeModal(activeModal);
            return;
        }
        if (e.key === 'Tab') {
            var focusables = getFocusable(activeModal);
            if (!focusables.length) return;
            var first = focusables[0];
            var last = focusables[focusables.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault(); last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault(); first.focus();
            }
        }
    });

    // Upload modal hookup (replace any earlier handlers with proper open/close)
    var uploadModalEl = document.getElementById('uploadModal');
    var uploadBtnEl = document.getElementById('uploadBtn');
    if (uploadBtnEl && uploadModalEl) {
        uploadBtnEl.addEventListener('click', function() { window.openModal(uploadModalEl); });
        uploadModalEl.addEventListener('click', function(e) {
            if (e.target === uploadModalEl) window.closeModal(uploadModalEl);
        });
    }
    document.querySelectorAll('.modal-overlay .close-modal').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var modal = btn.closest('.modal-overlay');
            if (modal) window.closeModal(modal);
        });
    });
    // "data-open-upload" hook from empty state
    document.querySelectorAll('[data-open-upload]').forEach(function(btn) {
        btn.addEventListener('click', function() { window.openModal(uploadModalEl); });
    });

    // ======== COMMAND PALETTE (Cmd/Ctrl+K) ========
    var cmdkModal = document.getElementById('cmdkModal');
    var cmdkInput = document.getElementById('cmdkInput');
    var cmdkList = document.getElementById('cmdkList');
    var headerSearchBtn = document.getElementById('headerSearchBtn');

    var COMMANDS = [
        { id: 'interactions', label: 'Go to Interactions', kind: 'View', icon: 'grid' },
        { id: 'action-items', label: 'Go to Action Items', kind: 'View', icon: 'check' },
        { id: 'live-call', label: 'Go to Live Call', kind: 'View', icon: 'mic' },
        { id: 'manager-monitoring', label: 'Go to Team Monitoring', kind: 'View', icon: 'eye' },
        { id: 'call-library', label: 'Go to Call Library', kind: 'View', icon: 'library' },
        { id: 'search', label: 'Go to Search', kind: 'View', icon: 'search' },
        { id: 'contacts', label: 'Go to Contacts', kind: 'View', icon: 'users' },
        { id: 'analytics', label: 'Go to Analytics', kind: 'View', icon: 'chart' },
        { id: 'scorecards', label: 'Go to Scorecards', kind: 'View', icon: 'clipboard' },
        { id: 'agent-performance', label: 'Go to Agent Performance', kind: 'View', icon: 'chart' },
        { id: 'knowledge-base', label: 'Go to Knowledge Base', kind: 'View', icon: 'book' },
        { id: 'integrations', label: 'Go to Integrations', kind: 'View', icon: 'plug' },
        { id: 'preferences', label: 'Go to Preferences', kind: 'View', icon: 'settings' },
        { id: '__upload', label: 'Upload a call recording', kind: 'Action', icon: 'upload' },
        { id: '__theme', label: 'Toggle color theme', kind: 'Action', icon: 'theme' },
        { id: '__sidebar', label: 'Toggle sidebar', kind: 'Action', icon: 'sidebar' }
    ];
    var cmdkIdx = 0;
    var cmdkFiltered = COMMANDS.slice();

    function renderCmdkList() {
        if (!cmdkList) return;
        cmdkList.innerHTML = '';
        if (!cmdkFiltered.length) {
            var empty = document.createElement('li');
            empty.className = 'cmdk-empty';
            empty.textContent = 'No matches';
            cmdkList.appendChild(empty);
            return;
        }
        cmdkFiltered.forEach(function(cmd, i) {
            var li = document.createElement('li');
            li.setAttribute('role', 'option');
            li.setAttribute('aria-selected', i === cmdkIdx ? 'true' : 'false');
            li.setAttribute('data-cmd', cmd.id);
            li.innerHTML =
                '<span>' + escapeHtml(cmd.label) + '</span>' +
                '<span class="cmdk-kind">' + escapeHtml(cmd.kind) + '</span>';
            li.addEventListener('click', function() { runCmd(cmd); });
            cmdkList.appendChild(li);
        });
    }
    function filterCmdk(q) {
        q = (q || '').toLowerCase().trim();
        if (!q) cmdkFiltered = COMMANDS.slice();
        else cmdkFiltered = COMMANDS.filter(function(c) {
            return c.label.toLowerCase().indexOf(q) !== -1 || c.kind.toLowerCase().indexOf(q) !== -1;
        });
        cmdkIdx = 0;
        renderCmdkList();
    }
    function runCmd(cmd) {
        window.closeModal(cmdkModal);
        if (cmd.id === '__upload' && uploadModalEl) { window.openModal(uploadModalEl); return; }
        if (cmd.id === '__theme' && themeToggle) { themeToggle.click(); return; }
        if (cmd.id === '__sidebar' && sidebarBtn) { sidebarBtn.click(); return; }
        if (typeof window.switchView === 'function') window.switchView(cmd.id);
    }
    function openCmdk() {
        if (!cmdkModal) return;
        filterCmdk('');
        window.openModal(cmdkModal);
        if (cmdkInput) cmdkInput.value = '';
    }
    if (cmdkInput) {
        cmdkInput.addEventListener('input', function(e) { filterCmdk(e.target.value); });
        cmdkInput.addEventListener('keydown', function(e) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                cmdkIdx = Math.min(cmdkIdx + 1, cmdkFiltered.length - 1);
                renderCmdkList();
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                cmdkIdx = Math.max(cmdkIdx - 1, 0);
                renderCmdkList();
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (cmdkFiltered[cmdkIdx]) runCmd(cmdkFiltered[cmdkIdx]);
            }
        });
    }
    if (cmdkModal) {
        cmdkModal.addEventListener('click', function(e) {
            if (e.target === cmdkModal) window.closeModal(cmdkModal);
        });
    }
    if (headerSearchBtn) {
        headerSearchBtn.addEventListener('click', openCmdk);
    }
    document.addEventListener('keydown', function(e) {
        if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
            e.preventDefault();
            openCmdk();
        }
    });

    // ======== TOOLTIP ========
    var tipEl = document.getElementById('tooltip');
    function showTip(target) {
        if (!tipEl) return;
        var text = target.getAttribute('data-tip');
        if (!text) return;
        tipEl.textContent = text;
        var rect = target.getBoundingClientRect();
        tipEl.classList.add('visible');
        // Place below + centered, then clamp
        var top = rect.bottom + 6;
        var left = rect.left + rect.width / 2 - tipEl.offsetWidth / 2;
        left = Math.max(8, Math.min(left, window.innerWidth - tipEl.offsetWidth - 8));
        if (top + tipEl.offsetHeight > window.innerHeight - 8) {
            top = rect.top - tipEl.offsetHeight - 6;
        }
        tipEl.style.top = top + 'px';
        tipEl.style.left = left + 'px';
        tipEl.setAttribute('aria-hidden', 'false');
    }
    function hideTip() {
        if (!tipEl) return;
        tipEl.classList.remove('visible');
        tipEl.setAttribute('aria-hidden', 'true');
    }
    document.addEventListener('mouseover', function(e) {
        var t = e.target.closest('[data-tip]');
        if (t) showTip(t);
    });
    document.addEventListener('mouseout', function(e) {
        var t = e.target.closest('[data-tip]');
        if (t) hideTip();
    });
    document.addEventListener('focusin', function(e) {
        var t = e.target.closest('[data-tip]');
        if (t) showTip(t);
    });
    document.addEventListener('focusout', function(e) {
        var t = e.target.closest('[data-tip]');
        if (t) hideTip();
    });
});

// ======== SVG SPARKLINES (smooth mini charts) ========
function renderMiniCharts() {
    var svgNS = 'http://www.w3.org/2000/svg';
    document.querySelectorAll('.mini-chart').forEach(function(chart) {
        if (chart.querySelector('svg')) return;
        var width = 120;
        var height = 40;
        var points = 16;
        // Seed deterministic per card so it looks stable across reloads
        var seed = chart.dataset.seed || Math.random().toString(36).slice(2, 8);
        chart.dataset.seed = seed;
        var rand = seededRandom(seed);
        var values = [];
        var cur = 40 + rand() * 20;
        for (var i = 0; i < points; i++) {
            cur += (rand() - 0.5) * 16;
            cur = Math.max(10, Math.min(cur, height - 6));
            values.push(cur);
        }
        var step = width / (points - 1);
        var linePath = '';
        var areaPath = '';
        values.forEach(function(v, idx) {
            var x = idx * step;
            var y = height - v;
            linePath += (idx === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1) + ' ';
        });
        areaPath = linePath + 'L' + width + ',' + height + ' L0,' + height + ' Z';

        var svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
        svg.setAttribute('preserveAspectRatio', 'none');
        svg.setAttribute('aria-hidden', 'true');

        var defs = document.createElementNS(svgNS, 'defs');
        defs.innerHTML = '<linearGradient id="mini-' + seed + '" x1="0%" y1="0%" x2="0%" y2="100%">' +
            '<stop offset="0%" stop-color="var(--primary)" stop-opacity="0.45"/>' +
            '<stop offset="100%" stop-color="var(--primary)" stop-opacity="0"/>' +
            '</linearGradient>';
        svg.appendChild(defs);

        var area = document.createElementNS(svgNS, 'path');
        area.setAttribute('d', areaPath);
        area.setAttribute('fill', 'url(#mini-' + seed + ')');
        svg.appendChild(area);

        var line = document.createElementNS(svgNS, 'path');
        line.setAttribute('d', linePath);
        line.setAttribute('fill', 'none');
        line.setAttribute('stroke', 'var(--primary)');
        line.setAttribute('stroke-width', '1.5');
        line.setAttribute('stroke-linecap', 'round');
        line.setAttribute('stroke-linejoin', 'round');
        svg.appendChild(line);

        chart.appendChild(svg);
    });
}

function seededRandom(seed) {
    // Simple Mulberry32
    var h = 0;
    for (var i = 0; i < seed.length; i++) {
        h = Math.imul(31, h) + seed.charCodeAt(i) | 0;
    }
    return function() {
        h |= 0; h = h + 0x6D2B79F5 | 0;
        var t = Math.imul(h ^ h >>> 15, 1 | h);
        t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
        return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
}
