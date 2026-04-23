/**
 * LINDA — Sandbox Demo Interactions
 * Wires up every interactive control in demo.html so buttons and
 * inputs do exactly what they say, even when the backend is absent.
 */
(function () {
    'use strict';

    // ======== HELPERS ========

    function esc(str) {
        if (str === null || str === undefined) return '';
        var div = document.createElement('div');
        div.textContent = String(str);
        return div.innerHTML;
    }

    function showToast(message, variant) {
        variant = variant || 'info';
        var container = document.getElementById('toastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toastContainer';
            container.className = 'toast-container';
            document.body.appendChild(container);
        }
        var toast = document.createElement('div');
        toast.className = 'toast toast-' + variant;
        toast.textContent = message;
        container.appendChild(toast);
        requestAnimationFrame(function () { toast.classList.add('show'); });
        setTimeout(function () {
            toast.classList.remove('show');
            setTimeout(function () { toast.remove(); }, 300);
        }, 3200);
    }

    function ensureGenericModal() {
        var overlay = document.getElementById('genericModal');
        if (overlay) return overlay;

        overlay = document.createElement('div');
        overlay.id = 'genericModal';
        overlay.className = 'modal-overlay';
        overlay.innerHTML =
            '<div class="modal">' +
            '  <div class="modal-header">' +
            '    <h2 class="generic-modal-title"></h2>' +
            '    <button class="close-modal" type="button" aria-label="Close">&times;</button>' +
            '  </div>' +
            '  <div class="generic-modal-body"></div>' +
            '  <div class="generic-modal-actions"></div>' +
            '</div>';
        document.body.appendChild(overlay);

        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeGenericModal();
        });
        overlay.querySelector('.close-modal').addEventListener('click', closeGenericModal);
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && overlay.classList.contains('active')) closeGenericModal();
        });
        return overlay;
    }

    function openModal(opts) {
        var overlay = ensureGenericModal();
        overlay.querySelector('.generic-modal-title').textContent = opts.title || '';
        var body = overlay.querySelector('.generic-modal-body');
        body.innerHTML = '';
        if (typeof opts.body === 'string') {
            body.innerHTML = opts.body;
        } else if (opts.body instanceof Node) {
            body.appendChild(opts.body);
        }

        var actions = overlay.querySelector('.generic-modal-actions');
        actions.innerHTML = '';
        var buttons = opts.actions || [{ label: 'Close', kind: 'outline' }];
        buttons.forEach(function (a) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-' + (a.kind || 'primary') + ' btn-sm';
            btn.textContent = a.label;
            btn.addEventListener('click', function () {
                var keepOpen = a.onClick && a.onClick(overlay) === false;
                if (!keepOpen) closeGenericModal();
            });
            actions.appendChild(btn);
        });

        overlay.classList.add('active');
        setTimeout(function () {
            var firstInput = body.querySelector('input, textarea, select');
            if (firstInput) firstInput.focus();
        }, 50);

        return overlay;
    }

    function closeGenericModal() {
        var overlay = document.getElementById('genericModal');
        if (overlay) overlay.classList.remove('active');
    }

    function confirmAction(message, confirmLabel) {
        return new Promise(function (resolve) {
            openModal({
                title: 'Confirm',
                body: '<p style="font-size:.9rem;color:var(--text-main);line-height:1.55">' + esc(message) + '</p>',
                actions: [
                    { label: 'Cancel', kind: 'outline', onClick: function () { resolve(false); } },
                    { label: confirmLabel || 'Confirm', kind: 'primary', onClick: function () { resolve(true); } }
                ]
            });
        });
    }

    function formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    }

    function closeAllDropdowns(except) {
        document.querySelectorAll('.simple-dropdown.open').forEach(function (d) {
            if (d !== except) d.classList.remove('open');
        });
    }

    // ======== DROPDOWN HELPER ========

    function mountDropdown(host, items, onSelect, opts) {
        opts = opts || {};
        if (host.getAttribute('data-dd-bound') === '1') return;
        host.setAttribute('data-dd-bound', '1');

        if (getComputedStyle(host).position === 'static') host.style.position = 'relative';

        var menu = document.createElement('div');
        menu.className = 'simple-dropdown';
        function render() {
            menu.innerHTML = '';
            if (opts.heading) {
                var h = document.createElement('div');
                h.className = 'dd-heading';
                h.textContent = opts.heading;
                menu.appendChild(h);
            }
            items.forEach(function (item) {
                if (typeof item === 'string') {
                    item = { label: item, value: item };
                }
                var el = document.createElement('div');
                el.className = 'dd-item' + (item.active ? ' active' : '');
                if (item.html) { el.innerHTML = item.html; } else { el.textContent = item.label; }
                el.addEventListener('click', function (e) {
                    e.stopPropagation();
                    items.forEach(function (it) { if (typeof it !== 'string') it.active = false; });
                    if (typeof item !== 'string') item.active = true;
                    render();
                    if (onSelect) onSelect(item);
                    menu.classList.remove('open');
                });
                menu.appendChild(el);
            });
        }
        render();
        host.appendChild(menu);

        host.addEventListener('click', function (e) {
            if (e.target.closest('.simple-dropdown')) return;
            e.stopPropagation();
            var opening = !menu.classList.contains('open');
            closeAllDropdowns(menu);
            menu.classList.toggle('open', opening);
        });

        document.addEventListener('click', function () { menu.classList.remove('open'); });
    }

    // ======== UPLOAD MODAL (file picker + drag-drop + queue + process) ========

    function initUploadModal() {
        var modal = document.getElementById('uploadModal');
        if (!modal) return;

        var dropZone = document.getElementById('uploadDropZone');
        var fileInput = document.getElementById('uploadFileInput');
        var browseBtn = document.getElementById('browseFilesBtn');
        var queue = document.getElementById('uploadQueue');
        var actions = document.getElementById('uploadActions');
        var cancelBtn = document.getElementById('cancelUploadBtn');
        var processBtn = document.getElementById('processUploadBtn');

        var stagedFiles = [];

        function renderQueue() {
            queue.innerHTML = '';
            stagedFiles.forEach(function (f, idx) {
                var row = document.createElement('div');
                row.className = 'upload-file-row';
                row.innerHTML =
                    '<span class="file-name">' + esc(f.name) + '</span>' +
                    '<span class="file-size">' + formatFileSize(f.size) + '</span>' +
                    '<button type="button" class="remove-file" aria-label="Remove">&times;</button>';
                row.querySelector('.remove-file').addEventListener('click', function () {
                    stagedFiles.splice(idx, 1);
                    renderQueue();
                });
                queue.appendChild(row);
            });
            actions.style.display = stagedFiles.length ? 'flex' : 'none';
        }

        function addFiles(fileList) {
            var added = 0;
            Array.prototype.forEach.call(fileList, function (file) {
                if (file.size > 500 * 1024 * 1024) {
                    showToast('"' + file.name + '" exceeds 500MB limit', 'error');
                    return;
                }
                stagedFiles.push(file);
                added++;
            });
            if (added) {
                renderQueue();
                showToast(added + ' file' + (added === 1 ? '' : 's') + ' staged', 'info');
            }
        }

        if (browseBtn) browseBtn.addEventListener('click', function () { fileInput.click(); });
        if (fileInput) fileInput.addEventListener('change', function (e) {
            addFiles(e.target.files);
            fileInput.value = '';
        });

        if (dropZone) {
            ['dragenter', 'dragover'].forEach(function (ev) {
                dropZone.addEventListener(ev, function (e) {
                    e.preventDefault(); e.stopPropagation();
                    dropZone.classList.add('drag-over');
                });
            });
            ['dragleave', 'drop'].forEach(function (ev) {
                dropZone.addEventListener(ev, function (e) {
                    e.preventDefault(); e.stopPropagation();
                    dropZone.classList.remove('drag-over');
                });
            });
            dropZone.addEventListener('drop', function (e) {
                if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
            });
        }

        if (cancelBtn) cancelBtn.addEventListener('click', function () {
            stagedFiles = [];
            renderQueue();
            modal.classList.remove('active');
        });

        if (processBtn) processBtn.addEventListener('click', function () {
            if (!stagedFiles.length) return;
            var count = stagedFiles.length;
            injectProcessingRows(stagedFiles);
            stagedFiles = [];
            renderQueue();
            modal.classList.remove('active');
            showToast('Processing ' + count + ' file' + (count === 1 ? '' : 's') + '…', 'success');
        });

        // Reset queue when modal is re-opened
        var uploadOpenBtn = document.getElementById('uploadBtn');
        if (uploadOpenBtn) {
            uploadOpenBtn.addEventListener('click', function () {
                stagedFiles = [];
                renderQueue();
            });
        }
    }

    function injectProcessingRows(files) {
        var tbody = document.querySelector('#interactions .interactions-table tbody');
        if (!tbody) return;
        files.forEach(function (file) {
            var tr = document.createElement('tr');
            tr.className = 'interaction-row';
            tr.setAttribute('data-channel', 'voice');
            tr.innerHTML =
                '<td><svg class="expand-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg></td>' +
                '<td><span class="channel-icon-svg voice">📞</span></td>' +
                '<td class="fw-500">' + esc(file.name.replace(/\.[^/.]+$/, '')) + '</td>' +
                '<td>—</td>' +
                '<td><span class="topic-pill">Uploaded</span></td>' +
                '<td><span class="sentiment-badge neutral">—</span></td>' +
                '<td><div class="qa-score-bar"><span>—</span><div class="qa-bar-track"></div></div></td>' +
                '<td><span class="action-completion">—</span></td>' +
                '<td><span class="risk-flags"></span></td>' +
                '<td>—</td>' +
                '<td><span class="date-relative">just now</span></td>' +
                '<td><span class="status-badge" style="background:rgba(245,158,11,0.15);color:var(--accent-amber)">Processing</span></td>';
            tbody.insertBefore(tr, tbody.firstChild);

            setTimeout(function () {
                var badge = tr.querySelector('.status-badge');
                if (badge) {
                    badge.style.background = 'rgba(6,182,212,0.1)';
                    badge.style.color = 'var(--accent-cyan)';
                    badge.textContent = 'Analyzed';
                }
                showToast('"' + file.name + '" analyzed', 'success');
            }, 2400);
        });
    }

    // ======== LIBRARY FILTERS & CARD CLICKS ========

    function initLibrary() {
        var grid = document.querySelector('#call-library .library-grid');
        if (!grid) return;

        var filters = document.querySelectorAll('#call-library .library-filters .filter-select');
        var tagInput = document.querySelector('#call-library .filter-tags-input');

        function applyFilters() {
            var typeVal = filters[0] ? filters[0].value.toLowerCase() : 'all types';
            var qualityVal = filters[1] ? filters[1].value.toLowerCase() : 'all quality';
            var agentVal = filters[2] ? filters[2].value.toLowerCase() : 'all agents';
            var tagVal = tagInput ? tagInput.value.trim().toLowerCase() : '';

            var visible = 0;
            grid.querySelectorAll('.library-card').forEach(function (card) {
                var quality = (card.querySelector('.quality-badge') || {}).textContent || '';
                var source = (card.querySelector('.library-source') || {}).textContent || '';
                var tags = Array.prototype.map.call(card.querySelectorAll('.tag'), function (t) { return t.textContent.toLowerCase(); });

                var matchQuality = qualityVal.indexOf('all') === 0 || quality.toLowerCase().indexOf(qualityVal) !== -1;
                var matchType = typeVal.indexOf('all') === 0 ||
                    (typeVal === 'best practice' && quality.toLowerCase() === 'exemplary') ||
                    (typeVal === 'training' && quality.toLowerCase().indexOf('needs') !== -1) ||
                    (typeVal === 'flagged' && quality.toLowerCase() === 'flagged');
                var matchAgent = agentVal.indexOf('all') === 0 || source.toLowerCase().indexOf(agentVal) !== -1;
                var matchTag = !tagVal || tags.some(function (t) { return t.indexOf(tagVal) !== -1; });

                var show = matchQuality && matchType && matchAgent && matchTag;
                card.style.display = show ? '' : 'none';
                if (show) visible++;
            });

            // If everything was filtered out, show a tiny placeholder
            var empty = document.getElementById('libraryEmpty');
            if (visible === 0) {
                if (!empty) {
                    empty = document.createElement('div');
                    empty.id = 'libraryEmpty';
                    empty.style.cssText = 'grid-column:1/-1;padding:2rem;text-align:center;color:var(--text-muted);font-size:.9rem';
                    empty.textContent = 'No snippets match these filters.';
                    grid.appendChild(empty);
                }
            } else if (empty) {
                empty.remove();
            }
        }

        filters.forEach(function (sel) { sel.addEventListener('change', applyFilters); });
        if (tagInput) {
            tagInput.addEventListener('input', applyFilters);
            tagInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); applyFilters(); } });
        }

        // Play / card click → jump to interaction detail
        grid.addEventListener('click', function (e) {
            var card = e.target.closest('.library-card');
            if (!card) return;
            var title = (card.querySelector('.library-card-title') || {}).textContent || 'Library Snippet';
            var time = (card.querySelector('.library-time-range') || {}).textContent || '';
            var playBtn = e.target.closest('.library-play-btn');
            if (playBtn) {
                showToast('Playing: ' + title + (time ? ' (' + time + ')' : ''), 'info');
                return;
            }
            if (typeof window.switchView === 'function') window.switchView('interaction-detail');
            var h1 = document.querySelector('#interaction-detail .view-header h1');
            if (h1) h1.textContent = title;
        });
    }

    // ======== BIG SEARCH INPUT + TRANSCRIPT SEARCH ========

    function initSearchView() {
        var input = document.querySelector('#search .search-big-input input');
        if (!input) return;

        var results = document.querySelectorAll('#search .search-result-item');
        var countLabel = document.querySelector('#search .search-result-count');

        function filter() {
            var q = input.value.trim().toLowerCase();
            var visible = 0;
            results.forEach(function (r) {
                var text = r.textContent.toLowerCase();
                var show = !q || text.indexOf(q) !== -1;
                r.style.display = show ? '' : 'none';
                if (show) visible++;
            });
            if (countLabel) {
                countLabel.textContent = visible + ' result' + (visible === 1 ? '' : 's') + (q ? ' for "' + input.value + '"' : '');
            }
        }

        input.addEventListener('input', filter);
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); filter(); }
        });

        // Header search: ensure typing there actually updates results when switched
        var headerInput = document.querySelector('.header-search input');
        if (headerInput) {
            headerInput.addEventListener('input', function () {
                input.value = headerInput.value;
                if (document.getElementById('search').classList.contains('active')) filter();
            });
            headerInput.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    if (typeof window.switchView === 'function') window.switchView('search');
                    filter();
                }
            });
        }
    }

    function initTranscriptSearch() {
        document.querySelectorAll('.transcript-search input').forEach(function (input) {
            if (input.getAttribute('data-bound') === '1') return;
            input.setAttribute('data-bound', '1');
            input.addEventListener('input', function () {
                var q = input.value.trim().toLowerCase();
                var scope = input.closest('.transcript-content') || input.closest('.transcript-column') || document;
                scope.querySelectorAll('.transcript-entry').forEach(function (entry) {
                    var text = entry.textContent.toLowerCase();
                    entry.style.display = !q || text.indexOf(q) !== -1 ? '' : 'none';
                });
            });
        });
    }

    // ======== COMMENTS / WHISPER / FOLLOW-UP EMAIL ========

    function initComments() {
        document.querySelectorAll('.comment-input-row').forEach(function (row) {
            if (row.getAttribute('data-bound') === '1') return;
            row.setAttribute('data-bound', '1');
            var input = row.querySelector('.comment-input');
            var btn = row.querySelector('button');
            var list = (row.closest('.insight-card') || document).querySelector('.comments-list');
            if (!input || !btn || !list) return;

            function post() {
                var text = input.value.trim();
                if (!text) { input.focus(); return; }
                var item = document.createElement('div');
                item.className = 'comment-item';
                var now = new Date();
                var timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                item.innerHTML =
                    '<div class="comment-header">' +
                    '  <span class="comment-author">You</span>' +
                    '  <span class="comment-anchor">' + esc(timeStr) + '</span>' +
                    '</div>' +
                    '<div class="comment-text">' + esc(text) + '</div>';
                list.appendChild(item);
                input.value = '';
                showToast('Comment posted', 'success');
            }

            btn.addEventListener('click', post);
            input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); post(); } });
        });
    }

    function initWhisperSenders() {
        document.querySelectorAll('.whisper-input-row').forEach(function (row) {
            if (row.getAttribute('data-bound') === '1') return;
            row.setAttribute('data-bound', '1');
            var input = row.querySelector('.whisper-input');
            var btn = row.querySelector('button');
            if (!input || !btn) return;

            function send() {
                var text = input.value.trim();
                if (!text) { input.focus(); return; }
                var tray = document.querySelector('#live-call .whisper-tray');
                if (tray) {
                    tray.classList.add('has-messages');
                    var icon = tray.querySelector('.whisper-icon');
                    if (icon) icon.textContent = '🔈';
                    var label = tray.querySelector('.whisper-label');
                    if (label) {
                        label.innerHTML = '<strong>Whisper:</strong> <span class="whisper-message">' + esc(text) + '</span>';
                    }
                }
                input.value = '';
                showToast('Whisper sent to agent', 'success');
            }

            btn.addEventListener('click', send);
            input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); send(); } });
        });
    }

    function initFollowUpEmail() {
        document.querySelectorAll('#interaction-detail .btn-primary.btn-block').forEach(function (btn) {
            if (btn.getAttribute('data-bound') === '1') return;
            if ((btn.textContent || '').toLowerCase().indexOf('follow-up') === -1) return;
            btn.setAttribute('data-bound', '1');

            btn.addEventListener('click', function () {
                var title = (document.querySelector('#interaction-detail .view-header h1') || {}).textContent || 'Follow-up';
                var bodyText =
                    'Hi there,\n\n' +
                    'Thank you for the time on our call today. As promised, here are the key takeaways and next steps we discussed:\n\n' +
                    '• Enterprise pricing breakdown with volume discounts\n' +
                    '• Proposed SSO/SAML and audit-log rollout timeline\n' +
                    '• Integration plan with your existing CRM\n\n' +
                    'Please let me know if any of this needs adjusting, and I will have updated materials over by tomorrow.\n\n' +
                    'Best,\nSarah';

                var bodyHtml =
                    '<label>To<input type="text" value="prospect@techventure.com"></label>' +
                    '<label>Subject<input type="text" value="Follow-up: ' + esc(title) + '"></label>' +
                    '<label>Message<textarea>' + esc(bodyText) + '</textarea></label>' +
                    '<p style="font-size:.75rem;color:var(--text-muted);margin:0">Draft generated from interaction insights. Review before sending.</p>';

                openModal({
                    title: 'Generate Follow-up Email',
                    body: bodyHtml,
                    actions: [
                        { label: 'Regenerate', kind: 'outline', onClick: function () { showToast('Regenerating draft…', 'info'); return false; } },
                        { label: 'Save Draft', kind: 'outline', onClick: function () { showToast('Draft saved', 'success'); } },
                        { label: 'Send', kind: 'primary', onClick: function () { showToast('Email sent', 'success'); } }
                    ]
                });
            });
        });
    }

    // ======== NOTIFICATION BELL + DATE PICKERS ========

    function initNotificationBell() {
        var bell = document.querySelector('.notification-bell');
        if (!bell || bell.getAttribute('data-bound') === '1') return;
        bell.setAttribute('data-bound', '1');

        var notifications = [
            { text: 'Sarah M. closed Acme Corp deal — $48k ARR', time: '2m ago' },
            { text: 'Flagged call: compliance gap detected', time: '14m ago' },
            { text: 'New coaching suggestion for James T.', time: '1h ago' }
        ];

        var items = notifications.map(function (n) {
            return {
                html: '<span class="notif-dot"></span><div><div style="color:var(--text-main);margin-bottom:2px">' + esc(n.text) + '</div><div style="font-size:.7rem;color:var(--text-muted)">' + esc(n.time) + '</div></div>'
            };
        });
        items.push({ label: 'Mark all as read', active: false });

        mountDropdown(bell, items, function (item) {
            if (item.label === 'Mark all as read') {
                var badge = bell.querySelector('.badge');
                if (badge) badge.remove();
                showToast('All notifications marked as read', 'success');
            }
        }, { heading: 'Notifications' });

        // Wrap notification items with nicer class
        setTimeout(function () {
            bell.querySelectorAll('.simple-dropdown .dd-item').forEach(function (i) {
                if (i.querySelector('.notif-dot')) i.classList.add('notification-item');
            });
        }, 0);
    }

    function initDatePickers() {
        var ranges = ['Last 7 Days', 'Last 30 Days', 'Last 90 Days', 'This Quarter', 'Year to Date', 'Custom range…'];
        document.querySelectorAll('.date-picker').forEach(function (dp) {
            var current = (dp.textContent || '').replace(/[▾▼\u25BE]/g, '').trim();
            var items = ranges.map(function (r) { return { label: r, active: r === current }; });
            mountDropdown(dp, items, function (chosen) {
                dp.innerHTML = esc(chosen.label) + ' <span style="opacity:.6">▾</span>';
                showToast('Range set: ' + chosen.label, 'info');
            });
        });
    }

    // ======== MONITORING (Manager view) ========

    function initMonitoring() {
        document.querySelectorAll('#manager-monitoring .monitoring-card').forEach(function (card) {
            var btn = card.querySelector('button');
            if (!btn || btn.getAttribute('data-bound') === '1') return;
            btn.setAttribute('data-bound', '1');

            btn.addEventListener('click', function () {
                var agentName = (card.querySelector('.monitoring-agent-name') || {}).textContent || 'Agent';
                var caller = (card.querySelector('.monitoring-caller') || {}).textContent || '';
                var panel = document.querySelector('#manager-monitoring .monitoring-detail-panel');
                if (panel) {
                    var h2 = panel.querySelector('h2');
                    if (h2) h2.innerHTML = 'Monitoring: ' + esc(agentName) + ' &rarr; ' + esc(caller.split(',')[0]);
                    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
                showToast('Now monitoring ' + agentName, 'info');
            });
        });
    }

    // ======== SCORECARDS ========

    function initScorecards() {
        var list = document.querySelector('.scorecard-templates-list');
        if (!list) return;

        list.addEventListener('click', function (e) {
            var item = e.target.closest('.template-item');
            if (!item) return;
            list.querySelectorAll('.template-item').forEach(function (t) { t.classList.remove('active'); });
            item.classList.add('active');
            var name = (item.querySelector('.template-name') || {}).textContent || 'Template';
            var editorHeader = document.querySelector('.scorecard-editor-preview .insight-card h3');
            if (editorHeader) editorHeader.textContent = name + ' Template';
        });

        document.querySelectorAll('#scorecards .btn-primary').forEach(function (btn) {
            if ((btn.textContent || '').indexOf('New Template') === -1) return;
            if (btn.getAttribute('data-bound') === '1') return;
            btn.setAttribute('data-bound', '1');

            btn.addEventListener('click', function () {
                openModal({
                    title: 'New Scorecard Template',
                    body:
                        '<label>Template name<input type="text" id="tplName" placeholder="e.g. Retention QA"></label>' +
                        '<label>Channel<select id="tplChannel"><option value="all">All channels</option><option value="voice">Voice</option><option value="chat">Chat</option><option value="email">Email</option></select></label>' +
                        '<label>Default criteria<textarea id="tplCriteria">Greeting & Rapport (20%)\nNeeds Discovery (25%)\nProduct Knowledge (20%)\nObjection Handling (20%)\nClose & Next Steps (15%)</textarea></label>',
                    actions: [
                        { label: 'Cancel', kind: 'outline' },
                        {
                            label: 'Create Template',
                            kind: 'primary',
                            onClick: function (overlay) {
                                var name = (overlay.querySelector('#tplName') || {}).value || 'New Template';
                                var item = document.createElement('div');
                                item.className = 'template-item';
                                item.innerHTML =
                                    '<span class="template-icon">📋</span>' +
                                    '<div>' +
                                    '  <span class="template-name">' + esc(name) + '</span>' +
                                    '  <span class="template-meta">5 criteria · Just created</span>' +
                                    '</div>';
                                list.appendChild(item);
                                showToast('Template "' + name + '" created', 'success');
                            }
                        }
                    ]
                });
            });
        });
    }

    // ======== KNOWLEDGE BASE ========

    function initKnowledgeBase() {
        var kbList = document.querySelector('.kb-document-list');
        var newArticleBtn, uploadDocBtn;
        document.querySelectorAll('#knowledge-base .header-actions-group button').forEach(function (b) {
            var txt = (b.textContent || '').toLowerCase();
            if (txt.indexOf('new article') !== -1) newArticleBtn = b;
            if (txt.indexOf('upload document') !== -1) uploadDocBtn = b;
        });

        if (newArticleBtn) newArticleBtn.addEventListener('click', function () {
            openModal({
                title: 'New Knowledge Base Article',
                body:
                    '<label>Title<input type="text" id="kbTitle" placeholder="Article title"></label>' +
                    '<label>Tags (comma-separated)<input type="text" id="kbTags" placeholder="sales, onboarding"></label>' +
                    '<label>Content<textarea id="kbContent" placeholder="Write your article…"></textarea></label>',
                actions: [
                    { label: 'Cancel', kind: 'outline' },
                    {
                        label: 'Publish',
                        kind: 'primary',
                        onClick: function (overlay) {
                            var title = (overlay.querySelector('#kbTitle') || {}).value || 'Untitled Article';
                            prependKbDoc({ title: title, source: 'Article · Just created', icon: '📝', status: 'Indexed' });
                            showToast('Article "' + title + '" published', 'success');
                        }
                    }
                ]
            });
        });

        if (uploadDocBtn) uploadDocBtn.addEventListener('click', function () {
            openModal({
                title: 'Upload Knowledge Base Document',
                body:
                    '<label>Document name<input type="text" id="kbDocName" placeholder="e.g. Pricing Guide"></label>' +
                    '<label>Source<select id="kbSource"><option value="upload">Direct upload (PDF/DOCX)</option><option value="confluence">Confluence</option><option value="notion">Notion</option><option value="gdrive">Google Drive</option></select></label>' +
                    '<p style="font-size:.75rem;color:var(--text-muted);margin:0">Documents are chunked and embedded for retrieval across calls.</p>',
                actions: [
                    { label: 'Cancel', kind: 'outline' },
                    {
                        label: 'Upload',
                        kind: 'primary',
                        onClick: function (overlay) {
                            var name = (overlay.querySelector('#kbDocName') || {}).value || 'New Document';
                            var src = (overlay.querySelector('#kbSource') || {}).value || 'upload';
                            var iconMap = { upload: '📄', confluence: '🔗', notion: '📓', gdrive: '📁' };
                            prependKbDoc({
                                title: name,
                                source: (src === 'upload' ? 'PDF' : src) + ' · Just uploaded',
                                icon: iconMap[src] || '📄',
                                status: 'Syncing…',
                                statusClass: 'syncing'
                            });
                            showToast('Uploading "' + name + '"…', 'info');
                            setTimeout(function () {
                                var row = document.querySelector('.kb-document[data-fresh="1"]');
                                if (row) {
                                    var status = row.querySelector('.kb-sync-status');
                                    if (status) {
                                        status.textContent = 'Indexed';
                                        status.classList.remove('syncing');
                                        status.classList.add('synced');
                                    }
                                    row.removeAttribute('data-fresh');
                                }
                            }, 2500);
                        }
                    }
                ]
            });
        });

        function prependKbDoc(doc) {
            if (!kbList) return;
            var row = document.createElement('div');
            row.className = 'kb-document';
            row.setAttribute('data-fresh', '1');
            row.innerHTML =
                '<span class="kb-source-icon">' + esc(doc.icon) + '</span>' +
                '<div class="kb-doc-info">' +
                '  <span class="kb-doc-title">' + esc(doc.title) + '</span>' +
                '  <span class="kb-doc-meta">' + esc(doc.source) + '</span>' +
                '</div>' +
                '<span class="kb-sync-status ' + (doc.statusClass || 'synced') + '">' + esc(doc.status) + '</span>';
            kbList.insertBefore(row, kbList.firstChild);
        }

        if (kbList) {
            kbList.addEventListener('click', function (e) {
                var doc = e.target.closest('.kb-document');
                if (!doc) return;
                var title = (doc.querySelector('.kb-doc-title') || {}).textContent || 'Document';
                showToast('Opening "' + title + '"', 'info');
            });
        }
    }

    // ======== INTEGRATIONS ========

    function initIntegrations() {
        document.querySelectorAll('#integrations .integration-card-item').forEach(function (card) {
            var btn = card.querySelector('button');
            if (!btn || btn.getAttribute('data-bound') === '1') return;
            btn.setAttribute('data-bound', '1');

            btn.addEventListener('click', function () {
                var name = (card.querySelector('.integration-name') || {}).textContent || 'Integration';
                if (btn.textContent.trim().toLowerCase() === 'connect') {
                    btn.disabled = true;
                    btn.textContent = 'Connecting…';
                    setTimeout(function () {
                        card.classList.add('connected');
                        btn.remove();
                        var badge = document.createElement('span');
                        badge.className = 'integration-status connected';
                        badge.textContent = 'Connected';
                        card.appendChild(badge);
                        showToast(name + ' connected', 'success');
                    }, 800);
                }
            });
        });

        // Let the toggle switches announce state changes
        document.querySelectorAll('#integrations .toggle-switch input').forEach(function (cb) {
            if (cb.getAttribute('data-bound') === '1') return;
            cb.setAttribute('data-bound', '1');
            cb.addEventListener('change', function () {
                var card = cb.closest('.integration-card-item');
                var name = card ? (card.querySelector('.integration-name') || {}).textContent : 'Integration';
                showToast(name + (cb.checked ? ' enabled' : ' disabled'), 'info');
            });
        });
    }

    // ======== API KEYS & WEBHOOKS ========

    function initApiKeys() {
        var generateBtn;
        document.querySelectorAll('#preferences .settings-section').forEach(function (sec) {
            var heading = (sec.querySelector('h3') || {}).textContent || '';
            if (heading.trim() === 'API Keys') {
                sec.querySelectorAll('button').forEach(function (b) {
                    var t = (b.textContent || '').toLowerCase();
                    if (t.indexOf('generate') !== -1) generateBtn = b;
                    if (t.indexOf('revoke') !== -1 && b.getAttribute('data-bound') !== '1') {
                        b.setAttribute('data-bound', '1');
                        b.addEventListener('click', function () {
                            var row = b.closest('tr');
                            var keyName = row ? (row.querySelector('td.fw-500') || {}).textContent : 'key';
                            confirmAction('Revoke the "' + keyName + '" API key? Requests using this key will immediately stop working.', 'Revoke').then(function (ok) {
                                if (ok && row) {
                                    row.remove();
                                    showToast('Key "' + keyName + '" revoked', 'warning');
                                }
                            });
                        });
                    }
                });
            }
        });

        if (generateBtn && generateBtn.getAttribute('data-bound') !== '1') {
            generateBtn.setAttribute('data-bound', '1');
            generateBtn.addEventListener('click', function () {
                openModal({
                    title: 'Generate New API Key',
                    body:
                        '<label>Key name<input type="text" id="newKeyName" placeholder="e.g. Integrations Server"></label>' +
                        '<label>Scopes<select id="newKeyScopes"><option>read-only</option><option>read-write</option><option selected>full-access</option></select></label>',
                    actions: [
                        { label: 'Cancel', kind: 'outline' },
                        {
                            label: 'Generate',
                            kind: 'primary',
                            onClick: function (overlay) {
                                var name = (overlay.querySelector('#newKeyName') || {}).value || 'Untitled';
                                var scope = (overlay.querySelector('#newKeyScopes') || {}).value || 'full-access';
                                var key = 'cs_live_' + Math.random().toString(36).slice(2, 10) + Math.random().toString(36).slice(2, 10);
                                showGeneratedKey(name, key, scope);
                                return false;
                            }
                        }
                    ]
                });
            });
        }

        function showGeneratedKey(name, key, scope) {
            openModal({
                title: 'API Key Created',
                body:
                    '<p style="font-size:.85rem;margin:0 0 .5rem;color:var(--accent-amber)">Copy this key now — it will never be shown again.</p>' +
                    '<div class="generated-key-display"><span id="keyValueDisplay">' + esc(key) + '</span>' +
                    '<button type="button" id="copyKeyBtn">Copy</button></div>' +
                    '<p style="font-size:.75rem;color:var(--text-muted);margin:0">Scope: ' + esc(scope) + '</p>',
                actions: [
                    {
                        label: 'Done',
                        kind: 'primary',
                        onClick: function () {
                            addApiKeyRow(name, key);
                            showToast('API key "' + name + '" created', 'success');
                        }
                    }
                ]
            });
            var overlay = document.getElementById('genericModal');
            var copyBtn = overlay && overlay.querySelector('#copyKeyBtn');
            if (copyBtn) copyBtn.addEventListener('click', function () {
                try {
                    navigator.clipboard.writeText(key);
                    copyBtn.textContent = 'Copied!';
                    setTimeout(function () { copyBtn.textContent = 'Copy'; }, 1500);
                } catch (e) { showToast('Could not copy — select and copy manually', 'warning'); }
            });
        }

        function addApiKeyRow(name, key) {
            var tbody;
            document.querySelectorAll('#preferences .settings-section').forEach(function (sec) {
                var heading = (sec.querySelector('h3') || {}).textContent || '';
                if (heading.trim() === 'API Keys') tbody = sec.querySelector('tbody');
            });
            if (!tbody) return;
            var masked = key.slice(0, 8) + '****...' + key.slice(-4);
            var today = new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
            var tr = document.createElement('tr');
            tr.innerHTML =
                '<td class="fw-500">' + esc(name) + '</td>' +
                '<td><code>' + esc(masked) + '</code></td>' +
                '<td>' + esc(today) + '</td>' +
                '<td>—</td>' +
                '<td><button class="btn btn-ghost btn-sm" type="button">Revoke</button></td>';
            tbody.appendChild(tr);
            var revokeBtn = tr.querySelector('button');
            revokeBtn.setAttribute('data-bound', '1');
            revokeBtn.addEventListener('click', function () {
                confirmAction('Revoke the "' + name + '" API key?', 'Revoke').then(function (ok) {
                    if (ok) { tr.remove(); showToast('Key "' + name + '" revoked', 'warning'); }
                });
            });
        }
    }

    function initWebhooks() {
        var webhooksSection;
        document.querySelectorAll('#preferences .settings-section').forEach(function (sec) {
            var heading = (sec.querySelector('h3') || {}).textContent || '';
            if (heading.trim() === 'Webhook Endpoints') webhooksSection = sec;
        });
        if (!webhooksSection) return;

        var addBtn;
        webhooksSection.querySelectorAll('button').forEach(function (b) {
            var txt = (b.textContent || '').toLowerCase();
            if (txt.indexOf('add endpoint') !== -1) addBtn = b;
            if (txt.trim() === 'edit' && b.getAttribute('data-bound') !== '1') {
                b.setAttribute('data-bound', '1');
                b.addEventListener('click', function () { openWebhookModal(b.closest('tr')); });
            }
        });

        if (addBtn && addBtn.getAttribute('data-bound') !== '1') {
            addBtn.setAttribute('data-bound', '1');
            addBtn.addEventListener('click', function () { openWebhookModal(null); });
        }

        function openWebhookModal(row) {
            var existingUrl = row ? (row.querySelector('code') || {}).textContent : '';
            var existingEvents = row ? (row.querySelectorAll('td')[1] || {}).textContent : '';

            openModal({
                title: row ? 'Edit Webhook' : 'Add Webhook',
                body:
                    '<label>Endpoint URL<input type="url" id="whUrl" value="' + esc(existingUrl) + '" placeholder="https://hooks.yoursite.com/linda"></label>' +
                    '<label>Events (comma-separated)<input type="text" id="whEvents" value="' + esc(existingEvents || 'call.completed, transcript.ready') + '"></label>' +
                    '<label style="flex-direction:row;align-items:center;gap:.5rem"><input type="checkbox" id="whActive" checked> Active</label>',
                actions: [
                    { label: 'Cancel', kind: 'outline' },
                    {
                        label: row ? 'Save' : 'Add',
                        kind: 'primary',
                        onClick: function (overlay) {
                            var url = (overlay.querySelector('#whUrl') || {}).value;
                            var events = (overlay.querySelector('#whEvents') || {}).value;
                            if (!url) { showToast('URL required', 'error'); return false; }
                            if (row) {
                                row.querySelector('code').textContent = url;
                                row.querySelectorAll('td')[1].textContent = events;
                                showToast('Webhook updated', 'success');
                            } else {
                                var tbody = webhooksSection.querySelector('tbody');
                                var tr = document.createElement('tr');
                                tr.innerHTML =
                                    '<td><code>' + esc(url) + '</code></td>' +
                                    '<td>' + esc(events) + '</td>' +
                                    '<td><span class="status-badge complete">Active</span></td>' +
                                    '<td><button class="btn btn-ghost btn-sm" type="button">Edit</button></td>';
                                tbody.appendChild(tr);
                                var editBtn = tr.querySelector('button');
                                editBtn.setAttribute('data-bound', '1');
                                editBtn.addEventListener('click', function () { openWebhookModal(tr); });
                                showToast('Webhook added', 'success');
                            }
                        }
                    }
                ]
            });
        }
    }

    // ======== CONTACTS: ADD + ROW CLICK ========

    function initContacts() {
        var addBtn;
        document.querySelectorAll('#contacts button').forEach(function (b) {
            if ((b.textContent || '').indexOf('Add Contact') !== -1) addBtn = b;
        });
        if (addBtn && addBtn.getAttribute('data-bound') !== '1') {
            addBtn.setAttribute('data-bound', '1');
            addBtn.addEventListener('click', function () {
                openModal({
                    title: 'New Contact',
                    body:
                        '<label>Full name<input type="text" id="cName" placeholder="Jane Doe"></label>' +
                        '<label>Company<input type="text" id="cCustomer" placeholder="Acme Corp"></label>' +
                        '<label>Phone<input type="text" id="cPhone" placeholder="+1 (555) 123-4567"></label>' +
                        '<label>Email<input type="email" id="cEmail" placeholder="jane@acme.com"></label>',
                    actions: [
                        { label: 'Cancel', kind: 'outline' },
                        {
                            label: 'Create',
                            kind: 'primary',
                            onClick: function (overlay) {
                                var name = (overlay.querySelector('#cName') || {}).value;
                                if (!name) { showToast('Name required', 'error'); return false; }
                                var company = (overlay.querySelector('#cCustomer') || {}).value || '—';
                                var phone = (overlay.querySelector('#cPhone') || {}).value || '—';
                                var email = (overlay.querySelector('#cEmail') || {}).value || '—';
                                var tbody = document.querySelector('#contacts .data-table tbody');
                                if (tbody) {
                                    var tr = document.createElement('tr');
                                    tr.className = 'clickable-row';
                                    tr.setAttribute('data-target', 'contact-detail');
                                    tr.innerHTML =
                                        '<td class="fw-500">' + esc(name) + '</td>' +
                                        '<td>' + esc(company) + '</td>' +
                                        '<td>' + esc(phone) + '</td>' +
                                        '<td>' + esc(email) + '</td>' +
                                        '<td>0</td>' +
                                        '<td>Just now</td>' +
                                        '<td><svg viewBox="0 0 80 20" class="sparkline-svg inline-sparkline"><polyline points="0,10 20,10 40,10 60,10 80,10" fill="none" stroke="#94A3B8" stroke-width="2"/></svg></td>';
                                    tbody.insertBefore(tr, tbody.firstChild);
                                }
                                showToast('Contact "' + name + '" added', 'success');
                            }
                        }
                    ]
                });
            });
        }

        // Row click navigates to contact detail
        var table = document.querySelector('#contacts .data-table');
        if (table && table.getAttribute('data-bound') !== '1') {
            table.setAttribute('data-bound', '1');
            table.addEventListener('click', function (e) {
                var tr = e.target.closest('tr');
                if (!tr || !tr.parentElement || tr.parentElement.tagName !== 'TBODY') return;
                var name = (tr.querySelector('td.fw-500') || {}).textContent;
                if (typeof window.switchView === 'function') window.switchView('contact-detail');
                var h1 = document.querySelector('#contact-detail .view-header h1');
                if (h1 && name) h1.innerHTML = esc(name) + ' &mdash; ' + esc((tr.children[1] || {}).textContent || '');
            });
        }
    }

    // ======== PAGINATION + ACTION ITEM CHECKBOXES ========

    function initPagination() {
        document.querySelectorAll('.table-pagination').forEach(function (p) {
            if (p.getAttribute('data-bound') === '1') return;
            p.setAttribute('data-bound', '1');

            var buttons = p.querySelectorAll('button');
            var label = p.querySelector('span');
            var match = (label.textContent || '').match(/Page (\d+) of (\d+)/);
            var state = { page: match ? parseInt(match[1], 10) : 1, total: match ? parseInt(match[2], 10) : 1 };

            function update() {
                label.textContent = 'Page ' + state.page + ' of ' + state.total;
                buttons[0].disabled = state.page <= 1;
                buttons[1].disabled = state.page >= state.total;
            }

            buttons[0].addEventListener('click', function () { if (state.page > 1) { state.page--; update(); showToast('Page ' + state.page, 'info'); } });
            buttons[1].addEventListener('click', function () { if (state.page < state.total) { state.page++; update(); showToast('Page ' + state.page, 'info'); } });
            update();
        });
    }

    function initActionItemCheckboxes() {
        var tbody = document.querySelector('#action-items .interactions-table tbody');
        if (!tbody || tbody.getAttribute('data-bound') === '1') return;
        tbody.setAttribute('data-bound', '1');
        tbody.addEventListener('change', function (e) {
            var cb = e.target;
            if (cb.tagName !== 'INPUT' || cb.type !== 'checkbox') return;
            var row = cb.closest('tr');
            if (!row) return;
            if (cb.checked) {
                row.setAttribute('data-status', 'done');
                row.style.opacity = '0.6';
                var badge = row.querySelector('.status-badge');
                if (badge) { badge.className = 'status-badge complete'; badge.textContent = 'Done'; }
                showToast('Action item marked done', 'success');
            } else {
                row.setAttribute('data-status', 'pending');
                row.style.opacity = '';
                var pbadge = row.querySelector('.status-badge');
                if (pbadge) { pbadge.className = 'status-badge'; pbadge.textContent = 'Pending'; }
            }
        });
    }

    // ======== PREFERENCES: RADIOS + SELECTS + PII TOGGLES ========

    function initPreferences() {
        var radioGroup = document.querySelector('#preferences .radio-group');
        if (radioGroup && radioGroup.getAttribute('data-bound') !== '1') {
            radioGroup.setAttribute('data-bound', '1');
            radioGroup.addEventListener('change', function (e) {
                if (e.target.type !== 'radio') return;
                radioGroup.querySelectorAll('.radio-item').forEach(function (i) { i.classList.remove('active'); });
                var item = e.target.closest('.radio-item');
                if (item) item.classList.add('active');
                showToast('Automation level: ' + e.target.value, 'info');
            });
        }

        var transcriptSelect = document.querySelector('#preferences .settings-select');
        if (transcriptSelect && transcriptSelect.getAttribute('data-bound') !== '1') {
            transcriptSelect.setAttribute('data-bound', '1');
            transcriptSelect.addEventListener('change', function () {
                showToast('Transcription engine: ' + transcriptSelect.value, 'info');
            });
        }

        document.querySelectorAll('#preferences .settings-toggle-row .toggle-switch input, #preferences .pii-entities input').forEach(function (cb) {
            if (cb.getAttribute('data-bound') === '1') return;
            cb.setAttribute('data-bound', '1');
            cb.addEventListener('change', function () {
                var label = cb.closest('label') || cb.parentElement;
                var text = (label && label.textContent || 'Setting').trim();
                showToast(text + (cb.checked ? ' enabled' : ' disabled'), 'info');
            });
        });
    }

    // ======== BOOT ========

    document.addEventListener('DOMContentLoaded', function () {
        initUploadModal();
        initLibrary();
        initSearchView();
        initTranscriptSearch();
        initComments();
        initWhisperSenders();
        initFollowUpEmail();
        initNotificationBell();
        initDatePickers();
        initMonitoring();
        initScorecards();
        initKnowledgeBase();
        initIntegrations();
        initApiKeys();
        initWebhooks();
        initContacts();
        initPagination();
        initActionItemCheckboxes();
        initPreferences();
    });

    // Expose helpers for debugging / potential reuse
    window.LindaDemo = {
        showToast: showToast,
        openModal: openModal,
        closeModal: closeGenericModal,
        confirmAction: confirmAction
    };
})();
