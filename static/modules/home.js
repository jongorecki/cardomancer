// home.js — Welcome / landing tab behaviour
// =========================================================================
//
// The Home tab is the default landing surface (see templates/partials/
// _tab_home.html). This module is responsible for two things:
//
//   1. goToTab(href) — programmatic tab activation triggered by the
//      action tiles. We don't depend on the data-bs-toggle="tab"
//      attribute because the tiles are <button>s, not nav links, and
//      Bootstrap's Tab helper accepts any element with a matching
//      data-bs-target / href.
//
//   2. loadHomeStatus() — populate the three-cell status strip
//      (hardware, collection, last session) so the operator can tell
//      at-a-glance whether the kiosk is ready and how recently it was
//      used. Also reveals the getting-started banner when no saved
//      hardware setup exists yet.
//
// Both functions are idempotent. loadHomeStatus is called on page
// load AND every time the Home tab is shown (so coming back from a
// session refreshes the "last session" line).

/**
 * Activate the tab whose pane has the given href (`#tab-sort` etc.).
 * Used by the Home tab's action tiles, which are <button>s rather
 * than nav links and therefore don't carry data-bs-toggle="tab".
 *
 * Falls back to setting window.location.hash if Bootstrap's Tab
 * helper isn't loaded — keeps the click responsive even in a
 * degraded JS environment.
 */
function goToTab(href) {
    const triggerLink = document.querySelector(
        `#mainTabs a[href="${href}"]`
    );
    if (triggerLink && typeof bootstrap !== 'undefined' && bootstrap.Tab) {
        const tab = bootstrap.Tab.getOrCreateInstance(triggerLink);
        tab.show();
        return;
    }
    // Bootstrap not available — at minimum, click the nav link
    // (Bootstrap data-bs-toggle="tab" will pick it up if registered)
    if (triggerLink) {
        triggerLink.click();
        return;
    }
    // Last-resort fallback: navigate to the hash.
    window.location.hash = href;
}

/**
 * Fetch and render the home page's quick-status strip.
 *
 * Three calls in parallel (sort tab status, collection stats, recent
 * sessions). Each cell falls back to a friendly empty-state if its
 * source isn't yet populated.
 */
async function loadHomeStatus() {
    const hwEl   = document.getElementById('home-status-hardware');
    const colEl  = document.getElementById('home-status-collection');
    const lastEl = document.getElementById('home-status-last-session');

    // Hardware status — read from /api/status (the worker's overall
    // state). 'idle' means connected + ready; 'disconnected' means
    // no serial link; anything else is a transient state we surface
    // as-is so the operator knows.
    if (hwEl) {
        try {
            const s = await apiGet('/api/status');
            const state = (s && s.state) || 'unknown';
            if (state === 'idle' || state === 'sorting' || state === 'paused') {
                hwEl.textContent = 'Connected';
                hwEl.style.color = 'var(--accent-green)';
            } else if (state === 'disconnected') {
                hwEl.textContent = 'Not connected';
                hwEl.style.color = 'var(--text-muted)';
            } else if (state === 'estopped') {
                hwEl.textContent = 'E-stop active';
                hwEl.style.color = 'var(--accent-red)';
            } else {
                hwEl.textContent = state;
                hwEl.style.color = 'var(--text-secondary)';
            }
        } catch (_) {
            hwEl.textContent = '—';
        }
    }

    // Collection size — total unique cards in inventory.
    if (colEl) {
        try {
            const s = await apiGet('/api/collection/stats');
            const total = (s && s.total_cards) || 0;
            if (total > 0) {
                colEl.textContent = `${total.toLocaleString()} ${
                    total === 1 ? 'card' : 'cards'
                }`;
            } else {
                colEl.textContent = 'empty';
                colEl.style.color = 'var(--text-muted)';
            }
        } catch (_) {
            colEl.textContent = '—';
        }
    }

    // Most recent session (status cell) AND the recent-activity panel
    // (top 3 sessions). One fetch feeds both — request limit=3.
    if (lastEl) {
        try {
            const s = await apiGet('/api/collection/sessions?limit=3');
            const sessions = (s && s.sessions) || [];
            if (sessions.length === 0) {
                lastEl.textContent = 'never';
                lastEl.style.color = 'var(--text-muted)';
                _populateRecentActivity([]);
            } else {
                const last = sessions[0];
                const ts = last.start_time || last.end_time;
                lastEl.textContent = _humanRelative(ts) || 'recently';
                _populateRecentActivity(sessions);
            }
        } catch (_) {
            lastEl.textContent = '—';
            _populateRecentActivity([]);
        }
    }

    // Getting-started banner. Show when no calibration setup is
    // saved — a fresh-install signal. We probe /api/calibration/
    // last-setup; a 404 (or missing payload) means nothing's been
    // saved. The banner is hidden in the default markup so a
    // calibrated kiosk doesn't show it.
    const gs = document.getElementById('home-getting-started');
    if (gs) {
        try {
            const r = await fetch('/api/calibration/last-setup');
            if (!r.ok) {
                gs.style.display = '';
                return;
            }
            const payload = await r.json();
            // The /last-setup endpoint returns {} or {found: false}
            // when no setup exists. Anything else = calibrated.
            const hasSetup = payload && (
                payload.found === true ||
                Object.keys(payload).some(k => k !== 'found' && k !== 'error')
            );
            gs.style.display = hasSetup ? 'none' : '';
        } catch (_) {
            // Network error — keep the banner hidden to avoid
            // showing a "new install" prompt on a transient hiccup.
            gs.style.display = 'none';
        }
    }
}

/**
 * Convert an ISO timestamp into a short relative phrase.
 * "5 min ago" / "2 h ago" / "yesterday" / "Apr 14".
 * Used by loadHomeStatus's last-session cell.
 */
function _humanRelative(iso) {
    if (!iso) return '';
    const then = new Date(iso);
    if (isNaN(then.getTime())) return '';
    const now = new Date();
    const diffMs = now.getTime() - then.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin} min ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr} h ago`;
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay === 1) return 'yesterday';
    if (diffDay < 7) return `${diffDay} days ago`;
    // Beyond a week, fall back to an absolute MMM DD date.
    return then.toLocaleDateString(undefined, {
        month: 'short', day: 'numeric',
    });
}

// =========================================================================
// Recent activity panel
// =========================================================================
//
// Renders up to 3 most recent sessions as clickable rows below the
// action tiles. Hides the panel entirely when the list is empty so
// fresh installs don't see a placeholder "no sessions yet" card —
// the getting-started banner already covers that state.
//
// Clicking a row jumps to the Collection tab's session-history view
// (the existing past-sessions modal handles per-session detail).

function _populateRecentActivity(sessions) {
    const panel = document.getElementById('home-recent-activity');
    const list  = document.getElementById('home-recent-activity-list');
    if (!panel || !list) return;
    if (!sessions || sessions.length === 0) {
        panel.style.display = 'none';
        list.innerHTML = '';
        return;
    }

    const rows = sessions.slice(0, 3).map(s => {
        const id = s.id;
        const when = _humanRelative(s.start_time || s.end_time) || '—';
        const mode = (s.sort_mode || 'custom').toString().toUpperCase();
        const total = s.total_scans || 0;
        const recog = s.recognized || 0;
        // Compose a concise "N scans · M recognized" tail.
        const countLine = `<strong>${total.toLocaleString()}</strong>`
            + ` scans &middot; ${recog.toLocaleString()} matched`;
        // Click jumps to the existing past-sessions modal so the
        // operator can drill into per-card detail. The modal already
        // ships with home.js's app shell (openPastSessionsModal lives
        // in sortStage.js / sessionHistory.js).
        const onclick = (typeof openPastSessionsModal === 'function')
            ? `onclick="openPastSessionsModal(${id})"`
            : `onclick="goToTab('#tab-collection')"`;
        return `
            <li>
                <button type="button"
                        class="cm-home-recent-row"
                        ${onclick}
                        aria-label="Open session #${id}, ${when}, ${mode}">
                    <span class="cm-home-recent-when">${when}</span>
                    <span class="cm-home-recent-mode">${mode}</span>
                    <span class="cm-home-recent-count">${countLine}</span>
                    <span class="cm-home-recent-arrow">&rsaquo;</span>
                </button>
            </li>`;
    }).join('');

    list.innerHTML = rows;
    panel.style.display = '';
}
