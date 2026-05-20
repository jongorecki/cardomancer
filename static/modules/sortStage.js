// sortStage.js — Phase 4: staged Sort tab
//
// The Sort tab walks the user through three stages and only exposes
// controls relevant to the current one (per plans/sort_flow_stages.md):
//
//   pre      — configure bin queries / overflow / source. Big "Start".
//   running  — live camera, stats, bin tiles. Pause + Stop primary.
//   post     — session summary, review queue, "Start another" / "Edit".
//
// Sections in _tab_sort.html mark which stages they belong to via
//   data-stage="pre"          single-stage
//   data-stage="pre running"  multi-stage (space-separated)
//   data-stage="all"          always visible (header chrome etc.)
//
// CSS in style.css hides sections whose data-stage doesn't include the
// current stage. The active stage is the data-sort-stage attribute on
// the #tab-sort root.

let _currentStage = 'pre';

function _applyStage(stage) {
    _currentStage = stage;
    const root = document.getElementById('tab-sort');
    if (root) root.setAttribute('data-sort-stage', stage);
    if (typeof addLog === 'function') {
        addLog(`Sort stage: ${stage}`);
    }
}

function getSortStage() {
    return _currentStage;
}

// Public action: jump back to pre-sort. Used by the post-sort
// "Start another" / "Edit config" buttons. The "Start another"
// path expects the user to press Start again to launch; "Edit
// config" just gets them back to the configurator.
function returnToPreSort() {
    _applyStage('pre');
}

// Initial stage on page load. Trust the worker's reported state:
//   sorting / paused → user is mid-session, render running stage.
//   anything else    → no session, show the configurator.
//
// The DOMContentLoaded boot in app.js already polls /api/status and
// sets currentState; this function is called from there once the
// poll resolves so the data-sort-stage attribute is correct before
// any stage-tagged section renders.
function initSortStageFromState(workerState) {
    if (workerState === 'sorting' || workerState === 'paused') {
        _applyStage('running');
    } else {
        _applyStage('pre');
    }
}

// Stage transitions on session lifecycle events. Wired in socket.js:
//
//   session_started   → 'running'  (pre  → running)
//   session_ended     → 'post'     (running → post)
//   session_paused    → no change  (still running, just paused state)
//   sorter_state idle → 'post' if we were running, else stay
//
// The "session_resumed" frontend event doesn't exist as a backend
// emission; resume bumps state from 'paused' to 'sorting' which the
// existing sorter_state handler already updates. No stage change
// needed since paused is still a running stage.

function onSessionStarted() {
    _applyStage('running');
}

function onSessionEndedStageHook() {
    _applyStage('post');
}

// Populate the post-sort summary hero from the session_ended payload.
// Called from socket.js's session_ended handler. Defensive about
// missing fields — the worker emits a stripped-down payload if the
// tracker teardown failed (see web_worker.py:2918), so we treat
// every field as optional.
function renderPostSortSummary(data) {
    const safe = data || {};
    const totalEl = document.getElementById('post-sort-total-scans');
    const recogEl = document.getElementById('post-sort-recognized');
    const unrecogEl = document.getElementById('post-sort-unrecognized');
    if (totalEl) totalEl.textContent = String(safe.total_scans || 0);
    if (recogEl) recogEl.textContent = String(safe.recognized || 0);
    if (unrecogEl) unrecogEl.textContent = String(safe.unrecognized || 0);
    const reasonEl = document.getElementById('post-sort-end-reason');
    if (reasonEl) {
        // session_ended doesn't yet carry a reason; show the timestamp
        // for now and grow this as the worker exposes more.
        reasonEl.textContent = `Ended ${new Date().toLocaleTimeString()}`;
    }
    // Refresh the review-queue count badge if the helper is available.
    if (typeof loadDetectionReviewCounts === 'function') {
        try { loadDetectionReviewCounts(); } catch (_) {}
    }
    // Populate the inline post-sort review preview from the recent-session
    // endpoint. Doesn't block — runs in the background and replaces the
    // placeholder when results arrive.
    populatePostSortReviewQueue();
}

// Fetch + render the recent-session detection-review queue inside the
// post-sort hero. Reuses the same data the Setup queue uses but
// scoped to the just-ended session.
// "View past sessions" modal — invoked from the post-sort hero's
// header link. Pulls up to 25 recent sessions and renders a compact
// table. Reuses the existing GET /api/collection/sessions route.
async function openPastSessionsModal() {
    const modalEl = document.getElementById('past-sessions-modal');
    const body = document.getElementById('past-sessions-content');
    if (!modalEl || !body) return;
    if (typeof bootstrap !== 'undefined') {
        new bootstrap.Modal(modalEl).show();
    }
    if (typeof renderSkeleton === 'function') {
        renderSkeleton(body, { rows: 5 });
    }
    try {
        const data = await apiGet('/api/collection/sessions?limit=25');
        const sessions = (data && data.sessions) || [];
        if (sessions.length === 0) {
            if (typeof renderEmptyState === 'function') {
                renderEmptyState(body, {
                    sigil: 'spiral',
                    title: 'No past sessions yet.',
                    body: 'Run a sort session to start building history.',
                });
            } else {
                body.innerHTML = '<p class="text-muted">No past sessions yet.</p>';
            }
            return;
        }
        const fmt = (iso) => {
            if (!iso) return '<span class="text-muted">—</span>';
            try {
                return new Date(iso).toLocaleString();
            } catch (_) { return iso; }
        };
        const rows = sessions.map(s => {
            const dur = (s.start_time && s.end_time)
                ? _humanDuration(s.start_time, s.end_time)
                : '<span class="text-muted">in flight</span>';
            const total = s.total_scans || 0;
            const recognized = s.recognized || 0;
            const unrecognized = s.unrecognized || 0;
            const mode = escapeHtml(s.sort_mode || s.config_name || '—');
            return `<tr>
                <td class="small">#${s.id}</td>
                <td class="small">${fmt(s.start_time)}</td>
                <td class="small">${dur}</td>
                <td class="small text-truncate" style="max-width:160px;"
                    title="${escapeHtml(s.config_name || '')}">${mode}</td>
                <td class="small text-end">${total}</td>
                <td class="small text-end text-success">${recognized}</td>
                <td class="small text-end text-warning">${unrecognized}</td>
            </tr>`;
        }).join('');
        body.innerHTML = `
            <div class="table-responsive">
                <table class="table table-sm align-middle mb-0">
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>Started</th>
                            <th>Duration</th>
                            <th>Preset</th>
                            <th class="text-end">Cards</th>
                            <th class="text-end">ID'd</th>
                            <th class="text-end">Review</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    } catch (e) {
        if (typeof renderEmptyState === 'function') {
            renderEmptyState(body, {
                sigil: 'eye',
                title: 'Could not load session history.',
                body: 'Try again in a moment.',
            });
        }
    }
}

function _humanDuration(startIso, endIso) {
    try {
        const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
        if (!isFinite(ms) || ms < 0) return '—';
        const s = Math.round(ms / 1000);
        if (s < 60) return `${s}s`;
        const m = Math.floor(s / 60);
        if (m < 60) return `${m}m ${s % 60}s`;
        const h = Math.floor(m / 60);
        return `${h}h ${m % 60}m`;
    } catch (_) { return '—'; }
}

async function populatePostSortReviewQueue() {
    const wrap = document.getElementById('post-sort-review-inline');
    const list = document.getElementById('post-sort-review-inline-list');
    if (!wrap || !list) return;
    try {
        const data = await apiGet('/api/detection-reviews/recent?limit=8');
        const items = (data && data.items) || [];
        if (items.length === 0) {
            wrap.style.display = 'none';
            return;
        }
        list.innerHTML = items.map(it => {
            const variable = escapeHtml(it.variable || '?');
            const name = escapeHtml(it.name || 'Unrecognized');
            const setStr = it.set_code ? `<span class="text-muted ms-1">${escapeHtml(it.set_code.toUpperCase())}</span>` : '';
            const cn = it.collector_number ? `<span class="text-muted">${escapeHtml('#' + it.collector_number)}</span>` : '';
            const conf = (it.confidence !== null && it.confidence !== undefined)
                ? `<span class="text-muted ms-2 small">conf ${Number(it.confidence).toFixed(2)}</span>`
                : '';
            return `<li class="d-flex align-items-center gap-2 py-1">
                <span class="badge bg-secondary" style="font-size:0.65em">${variable}</span>
                <strong>${name}</strong>
                ${setStr}${cn}
                ${conf}
            </li>`;
        }).join('');
        wrap.style.display = '';
    } catch (e) {
        wrap.style.display = 'none';
    }
}

// Post-sort action: launch another session with the same preset.
// Calls the existing startSession() so identical to the pre-sort
// hero's button — there's no duplication of the start machinery.
function startAnotherSession() {
    if (typeof startSession === 'function') startSession();
}

// Post-sort action: jump to the Setup tab and surface the detection
// review queue. Setup's onclick already calls
// loadDetectionReviewCounts(), so the user lands on a populated queue.
function goToReviewQueue() {
    const setupLink = document.querySelector('a[href="#tab-setup"]');
    if (setupLink && typeof bootstrap !== 'undefined') {
        new bootstrap.Tab(setupLink).show();
    }
}

// When the detection-review counts refresh, mirror the pending count
// onto the post-sort hero's "Review uncertain cards" badge so the
// user knows whether it's worth clicking through.
function updatePostSortReviewBadge(pending) {
    const badge = document.getElementById('post-sort-review-count');
    if (!badge) return;
    if (pending && pending > 0) {
        badge.textContent = String(pending);
        badge.style.display = '';
    } else {
        badge.style.display = 'none';
    }
}

// Power-loss / unclean-shutdown limbo: user hit a startup state where
// a session was active but didn't end cleanly. The Sort tab should
// surface a "Resume?" prompt rather than dropping straight into
// either pre-sort (loses the session) or running (resumes silently
// without confirmation). For Phase 4 part 5; stub the hook so other
// modules can call it now.
function onUnfinishedSessionDetected() {
    // Phase 4 part 5 — full Resume rehydration. For now we route to
    // post-sort so the user sees a summary; the stale-session banner
    // (already in place from roadblock #3) gives them the discard
    // affordance until the dedicated limbo state ships.
    _applyStage('post');
}
