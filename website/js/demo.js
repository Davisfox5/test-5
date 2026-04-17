// ======== API CONFIGURATION ========
let API_CONNECTED = false;
let API_KEY = localStorage.getItem('callsight-api-key') || 'csk__aQLNT3-D21Yiyv60ffeAA9L8XUKVi5HOB58f0c_1wg';

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
 * Load interactions from API and rebuild the table tbody.
 */
async function loadInteractions(channel) {
    var path = '/interactions?limit=50';
    if (channel && channel !== 'all') {
        path += '&channel=' + encodeURIComponent(channel);
    }
    var data = await apiFetch(path);
    if (!data) return; // stay with mock HTML

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

        if (item.insights) {
            sentiment = item.insights.sentiment_score || item.insights.sentiment || 0;
            qaScore = item.insights.qa_score || 0;
            topics = item.insights.topics || [];
            summary = item.insights.summary || '';
        }
        if (item.call_metrics) {
            // call_metrics may have action item counts
        }
        if (item.action_items) {
            actionsTotal = item.action_items.length;
            actionsDone = item.action_items.filter(function(a) { return a.status === 'done'; }).length;
        }

        var topicsPills = topics.map(function(t) {
            return '<span class="topic-pill">' + escapeHtml(t) + '</span>';
        }).join('');

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
            '<td><svg class="expand-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg></td>' +
            '<td><span class="channel-icon-svg ' + escapeHtml(item.channel || 'voice') + '">' + (CHANNEL_ICONS[item.channel] || CHANNEL_ICONS.voice) + '</span></td>' +
            '<td class="fw-500 interaction-title" data-target="interaction-detail" data-interaction-id="' + item.id + '">' + escapeHtml(item.title || 'Untitled') + '</td>' +
            '<td>' + escapeHtml(contactName) + '</td>' +
            '<td>' + topicsPills + '</td>' +
            '<td><span class="sentiment-badge ' + sentCls + '">' + sentVal + '</span></td>' +
            '<td><div class="qa-score-bar"><span>' + qaScore + '</span><div class="qa-bar-track"><div class="qa-bar-fill ' + qaCls + '" style="width:' + qaScore + '%"></div></div></div></td>' +
            '<td><span class="action-completion ' + actionCls + '">' + actionLabel + '</span></td>' +
            '<td><span class="risk-flags"></span></td>' +
            '<td>' + duration + '</td>' +
            '<td><span class="date-relative" title="' + escapeHtml(item.created_at || '') + '">' + formatRelativeDate(item.created_at) + '</span></td>' +
            '<td>' + statusBadgeHtml(item.status || 'processing') + '</td>';
        tbody.appendChild(tr);

        // Expand row
        var expandTr = document.createElement('tr');
        expandTr.className = 'row-expand';
        expandTr.setAttribute('data-channel', item.channel || 'voice');
        expandTr.innerHTML =
            '<td colspan="12"><div class="row-expand-content">' +
            '<p class="row-expand-summary">' + escapeHtml(summary) + '</p>' +
            '<div class="row-expand-footer">' +
            '<span style="font-size:13px;color:var(--text-muted)">QA: <strong>' + qaScore + '/100</strong></span>' +
            '<button class="btn btn-primary btn-sm" onclick="loadInteractionDetail(\'' + item.id + '\')">View Full Detail &rarr;</button>' +
            '</div></div></td>';
        tbody.appendChild(expandTr);
    });

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
 * Load analytics from API.
 */
async function loadAnalytics() {
    var data = await apiFetch('/analytics/business');
    if (!data) return;

    // Update health score gauge
    if (data.health_score !== undefined) {
        var gaugeValue = document.querySelector('#analytics .gauge-value');
        if (gaugeValue) gaugeValue.textContent = Math.round(data.health_score);
    }

    // Update stat cards in analytics if they exist
    if (data.total_interactions !== undefined) {
        var statValues = document.querySelectorAll('#analytics .stat-value');
        // The analytics view may have stat cards too
    }

    if (data.avg_sentiment !== undefined) {
        // Update sentiment display if available
    }
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
    var savedGroups = JSON.parse(localStorage.getItem('callsight-nav-groups') || '{}');

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
                localStorage.setItem('callsight-nav-groups', JSON.stringify(states));
            });
        }
    });

    // ======== ROLE TOGGLE ========
    var roleToggle = document.getElementById('roleToggle');
    var appLayout = document.querySelector('.app-layout');
    var userRole = document.querySelector('.user-role');

    // Restore saved role
    if (localStorage.getItem('callsight-role') === 'manager') {
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
                localStorage.setItem('callsight-role', role);
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

    // ======== MODAL LOGIC ========
    var uploadBtn = document.getElementById('uploadBtn');
    var uploadModal = document.getElementById('uploadModal');

    if (uploadBtn && uploadModal) {
        uploadBtn.addEventListener('click', function() { uploadModal.classList.add('active'); });
        uploadModal.addEventListener('click', function(e) {
            if (e.target === uploadModal) uploadModal.classList.remove('active');
        });
    }

    document.querySelectorAll('.close-modal').forEach(function(btn) {
        btn.addEventListener('click', function() {
            if (uploadModal) uploadModal.classList.remove('active');
        });
    });

    // ======== HEADER SEARCH -> SEARCH VIEW ========
    var headerSearch = document.querySelector('.header-search input');
    if (headerSearch) {
        headerSearch.addEventListener('focus', function() { switchView('search'); });
    }

    // ======== MINI CHARTS ========
    document.querySelectorAll('.mini-chart').forEach(function(chart) {
        for (var i = 0; i < 20; i++) {
            var bar = document.createElement('div');
            bar.style.cssText =
                'width: 4px;' +
                'height: ' + (20 + Math.random() * 80) + '%;' +
                'background: var(--primary);' +
                'opacity: 0.3;' +
                'border-radius: 2px;' +
                'position: absolute;' +
                'bottom: 0;' +
                'left: ' + (i * 6) + 'px;';
            chart.appendChild(bar);
        }
    });
});
