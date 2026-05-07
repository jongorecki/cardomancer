// session.js — Sort session lifecycle + bin contents + continuous + wishlist/priority
// =========================================================================
// Test Scan
// =========================================================================

function startTestScan() {
    const count = parseInt(document.getElementById('test-scan-count').value) || 10;
    const drop_bin = parseInt(document.getElementById('test-scan-bin').value) || 1;
    document.getElementById('test-scan-results').innerHTML = '';
    document.getElementById('btn-test-scan-start').style.display = 'none';
    document.getElementById('btn-test-scan-stop').style.display = '';
    const prog = document.getElementById('test-scan-progress');
    prog.style.display = '';
    prog.textContent = 'Starting...';
    apiPost('/api/test-scan/start', { count, drop_bin });
}

function stopTestScan() {
    apiPost('/api/test-scan/stop');
}

function _testScanDone() {
    document.getElementById('btn-test-scan-start').style.display = '';
    document.getElementById('btn-test-scan-stop').style.display = 'none';
}

// =========================================================================
// Sort Session
// =========================================================================

async function startSession() {
    // Validate all bin queries before starting. Scroll to first failing row
    // and block start if any query is invalid.
    const valid = await _validateAllSortRows();
    if (!valid) {
        addLog('Fix invalid queries before starting a session');
        return;
    }

    const config_lines = serializeSortConfig();
    const payload = { config_lines };

    // Log the preset that's about to be applied so the user can confirm
    // the Dashboard dropdown actually took effect (this was a recurring
    // "why is it always sorting by color?" complaint before the dashboard
    // preset selector was added).
    if (_scFilename) {
        addLog(`Starting session with preset: ${_scFilename}`);
    } else {
        addLog('Starting session with unsaved sort config');
    }

    const notes = document.getElementById('session-notes')?.value?.trim();
    if (notes) payload.notes = notes;

    const plan = typeof getStoragePlan === 'function' ? getStoragePlan() : null;
    if (plan) payload.storage_plan = plan;

    const resortToggle = document.getElementById('resort-mode-toggle');
    const sourceSelect = document.getElementById('resort-source-select');
    if (resortToggle && resortToggle.checked && sourceSelect && sourceSelect.value) {
        payload.source_box = sourceSelect.value;
    }

    console.log('[startSession] payload:', JSON.stringify(payload));
    apiPost('/api/session/start', payload);
}

let _cachedBinDetails = {};  // full card lists per bin

// Phase 4 part 3 — bin tile row. Replaces the previous vertical
// expand/collapse list with a grid of bin tiles. Each tile shows:
//   - Bin number + (when applicable) FULL badge + fullness bar
//   - Per-bin Empty button (small, top-right corner)
//   - Live count vs. capacity
//   - Last card dropped (name + set + foil/price hint)
//   - Click to expand → reveals a scrollable strip of recent cards
//     in this bin (same hover-preview behavior as before).
//
// Existing callers (motion.js's _flushPendingBinUpdates,
// socket.js's session_started + bin_update handlers, simulation.js)
// keep calling this function with the same (cardsPerBin,
// binDetailsData, binFullness) signature — no plumbing changes.
function updateBinContentsPanel(cardsPerBin, binDetailsData, binFullness) {
    if (!cardsPerBin) return;
    _cachedBinCounts = cardsPerBin;
    if (binDetailsData) _cachedBinDetails = binDetailsData;
    const binsFull = new Set((binFullness && binFullness.bins_full) || []);
    const binCounts = (binFullness && binFullness.bin_card_counts) || {};
    const limit = (binFullness && binFullness.bin_card_limit) || 300;
    const panel = document.getElementById('session-bin-contents');
    if (!panel) return;

    const entries = Object.entries(cardsPerBin)
        .sort((a, b) => parseInt(a[0]) - parseInt(b[0]));

    if (entries.length === 0) {
        panel.innerHTML = '<p class="text-muted small mb-0">No cards sorted yet.</p>';
        drawMotionCanvas();
        drawBinLayoutCanvas();
        return;
    }

    // Preserve which bins were expanded across re-renders. Without
    // this every event would collapse everything mid-sort, which
    // disrupts the user's investigation of "what just went here?".
    const previouslyExpanded = new Set(
        Array.from(panel.querySelectorAll('.bin-tile-detail'))
             .filter(el => el.style.display !== 'none')
             .map(el => el.getAttribute('data-bin'))
    );

    let html = '<div class="bin-tile-grid">';
    for (const [bin, count] of entries) {
        const cards = _cachedBinDetails[bin] || [];
        const lastCard = cards.length > 0 ? cards[cards.length - 1] : null;
        const isFull = binsFull.has(parseInt(bin));
        const liveCount = binCounts[bin] || count;
        const pct = limit > 0 ? Math.min(100, Math.round((liveCount / limit) * 100)) : 0;
        const fullBadge = isFull
            ? ' <span class="badge bg-danger ms-1">FULL</span>' : '';
        const isExpanded = previouslyExpanded.has(String(bin));

        // Last-card line — set + name; foil tag if applicable.
        let lastCardLine = '';
        if (lastCard) {
            const foilTag = lastCard.is_foil
                ? ' <span class="badge bg-warning text-dark" style="font-size:0.6em">&#9733;</span>'
                : '';
            const setStr = lastCard.set ? lastCard.set.toUpperCase() : '';
            lastCardLine = `<div class="bin-tile-last small text-muted text-truncate"
                                 title="${escapeHtml(lastCard.name)}">
                <span class="text-secondary">last:</span>
                ${escapeHtml(lastCard.name)}${foilTag}
                ${setStr ? `<span class="ms-1 text-muted">${setStr}</span>` : ''}
            </div>`;
        } else {
            lastCardLine = `<div class="bin-tile-last small text-muted">empty</div>`;
        }

        // Fullness bar — subtle accent that flips warning > 75% and
        // danger when bin is marked full.
        const barClass = isFull ? 'bin-tile-bar-full'
            : (pct >= 75 ? 'bin-tile-bar-warn' : '');
        const barStyle = `width:${pct}%;`;

        // Recent-cards strip (hidden by default; click bin number / count
        // to reveal). Reuses the existing _scryfallImageUrl + hover
        // preview pattern.
        let cardListHtml = '';
        if (cards.length > 0) {
            for (const card of cards) {
                const canPreview = card.set && card.collector_number;
                const imgUrl = canPreview
                    ? _scryfallImageUrl(card.set, card.collector_number, 'normal')
                    : '';
                const hoverAttr = canPreview
                    ? `onmouseenter="_showCardPreview(event, '${imgUrl}', 'left')" onmouseleave="_hideCardPreview()" style="cursor:help"`
                    : '';
                const foilTag = card.is_foil
                    ? ' <span class="badge bg-warning text-dark" style="font-size:0.6em">&#9733;</span>'
                    : '';
                const priceTag = card.price && card.price !== 'null' && card.price !== 'N/A'
                    ? `<span class="text-success ms-1">${escapeHtml(String(card.price))}</span>`
                    : '';
                cardListHtml += `<div class="bin-tile-card-entry small d-flex justify-content-between align-items-center text-truncate"
                                      title="${escapeHtml(card.name)}" ${hoverAttr}>
                    <span class="text-truncate">
                        <span class="text-muted">#${card.scan_num}</span>
                        ${escapeHtml(card.name)}${foilTag}
                    </span>
                    <span class="flex-shrink-0 ms-2 text-muted">
                        ${card.set ? card.set.toUpperCase() : ''}${priceTag}
                    </span>
                </div>`;
            }
        }

        html += `
            <div class="bin-tile${isFull ? ' bin-tile-full' : ''}"
                 onclick="toggleBinDetail(${bin})">
                <div class="bin-tile-head d-flex justify-content-between align-items-center">
                    <strong>Bin ${bin}${fullBadge}</strong>
                    <button class="btn btn-outline-success btn-sm py-0 px-1"
                            onclick="event.stopPropagation(); markBinEmpty(${bin})"
                            title="Mark bin as emptied (resets card count)">Empty</button>
                </div>
                <div class="bin-tile-count">
                    <span class="bin-tile-count-num">${liveCount}</span>
                    <span class="bin-tile-count-limit text-muted small">/ ${limit}</span>
                </div>
                <div class="bin-tile-bar">
                    <div class="bin-tile-bar-fill ${barClass}" style="${barStyle}"></div>
                </div>
                ${lastCardLine}
                <div class="bin-tile-detail" data-bin="${bin}"
                     style="display:${isExpanded ? 'block' : 'none'};"
                     onclick="event.stopPropagation();">
                    ${cardListHtml || '<div class="small text-muted">No card detail yet.</div>'}
                </div>
            </div>`;
    }
    html += '</div>';
    panel.innerHTML = html;
    drawMotionCanvas();
    drawBinLayoutCanvas();
}

function toggleBinDetail(binNum) {
    const el = document.querySelector(`.bin-tile-detail[data-bin="${binNum}"]`);
    if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function toggleAllBinDetails() {
    const details = document.querySelectorAll('.bin-tile-detail');
    const anyHidden = [...details].some(el => el.style.display === 'none');
    details.forEach(el => { el.style.display = anyHidden ? 'block' : 'none'; });
}

// =========================================================================
// Continuous Sort
// =========================================================================

let _continuousActive = false;
let _undoAvailable = false;

function toggleContinuousSort() {
    if (_continuousActive) {
        apiPost('/api/session/continuous/stop');
    } else {
        const delay = parseFloat(document.getElementById('continuous-delay').value) || 0.5;
        apiPost('/api/session/continuous/start', { delay });
    }
}

function undoLastSort() {
    if (!_undoAvailable) return;
    apiPost('/api/session/undo');
    _undoAvailable = false;
    document.getElementById('btn-undo').disabled = true;
}

// =========================================================================
// Priority-bin toast (Phase 4.21)
// Non-modal, anchored to the bin-layout card.  Stays until dismissed.
// =========================================================================

function showPriorityToast(data) {
    const stack = document.getElementById('priority-toast-stack');
    if (!stack) return;

    const toast = document.createElement('div');
    toast.className = 'toast show border border-warning';
    toast.setAttribute('role', 'alert');
    toast.style.pointerEvents = 'auto';
    toast.style.minWidth = '260px';
    toast.style.backgroundColor = 'rgba(30, 30, 50, 0.97)';
    toast.style.color = '#fff';
    toast.style.boxShadow = '0 0 12px rgba(255, 193, 7, 0.6)';

    const img = data.image_uri
        ? `<img src="${escapeHtml(data.image_uri)}" alt="" style="width:60px; height:auto; border-radius:4px; margin-right:8px;">`
        : '';
    const setBadge = data.set
        ? `<span class="badge bg-secondary ms-1">${escapeHtml(String(data.set).toUpperCase())}</span>`
        : '';

    toast.innerHTML = `
        <div class="toast-header bg-warning text-dark">
            <strong class="me-auto">⭐ Wishlist Match</strong>
            <button type="button" class="btn-close" aria-label="Dismiss"></button>
        </div>
        <div class="toast-body d-flex align-items-start">
            ${img}
            <div class="flex-grow-1">
                <div><strong>${escapeHtml(data.name || 'Unknown')}</strong>${setBadge}</div>
                <div class="small text-warning">Routed to bin #${data.priority_bin}</div>
            </div>
        </div>
    `;
    toast.querySelector('.btn-close').addEventListener('click', () => {
        toast.remove();
    });
    stack.appendChild(toast);
}

// =========================================================================
// Wishlist Bin + Re-home Interval
// =========================================================================

function setWishlistBin() {
    const val = document.getElementById('wishlist-bin-input').value;
    const binNum = val ? parseInt(val) : null;
    apiPost('/api/session/wishlist-bin', { bin: binNum });
    addLog(binNum ? `Wishlist bin set to ${binNum}` : 'Wishlist bin disabled');
}

// --- Priority bin (Phase 4.21) ---

async function loadPriorityWishlistSources() {
    const sel = document.getElementById('priority-wishlist-select');
    if (!sel) return;
    try {
        const data = await apiGet('/api/integrations/moxfield/wishlist/list');
        const current = sel.value;
        sel.innerHTML = '<option value="">— wishlist —</option>';
        for (const w of (data.wishlists || [])) {
            const opt = document.createElement('option');
            opt.value = w.source_key;
            opt.textContent = `${w.display_name || w.source_key} (${w.card_count})`;
            sel.appendChild(opt);
        }
        if (current) sel.value = current;
    } catch (e) {
        console.warn('loadPriorityWishlistSources failed', e);
    }
}

function setPriorityBin() {
    const binVal = document.getElementById('priority-bin-input').value;
    const src = document.getElementById('priority-wishlist-select').value;
    const binNum = binVal ? parseInt(binVal) : null;
    const wishlistSource = src || null;
    apiPost('/api/session/priority-bin', {
        bin: binNum,
        wishlist_source: wishlistSource,
    });
    if (binNum && wishlistSource) {
        addLog(`Priority bin set to ${binNum} (wishlist: ${wishlistSource})`);
    } else {
        addLog('Priority bin disabled');
    }
}

function setRehomeInterval() {
    const val = parseInt(document.getElementById('rehome-interval').value) || 100;
    apiPost('/api/session/rehome-interval', { interval: val });
    addLog(`Re-home interval set to every ${val} cards`);
}

// =========================================================================
// Storage plan + resort toggle helpers
// =========================================================================

function getStoragePlan() {
    const box = document.getElementById('storage-box-select');
    const div = document.getElementById('storage-divider-input');
    if (!box || !div || !box.value || !div.value) return null;
    return { box: box.value, starting_divider: parseInt(div.value, 10) };
}

function onResortToggle() {
    const toggle = document.getElementById('resort-mode-toggle');
    const row = document.getElementById('resort-source-row');
    if (!toggle || !row) return;
    row.style.display = toggle.checked ? '' : 'none';
}

// =========================================================================
// Stale-session prompt (power-loss / unclean-shutdown recovery)
// =========================================================================
// Phase 2 ships detect + discard. Phase 4 will replace this banner with
// the staged Sort tab's "Resume?" limbo state per plans/sort_flow_stages.md.
//
// The banner is injected into the page at startup (so we don't need to
// touch every existing partial). It sits at the top of the body, above
// the navbar, and is dismissible without taking action.

function _ensureStaleSessionBanner() {
    let bar = document.getElementById('stale-session-banner');
    if (bar) return bar;
    bar = document.createElement('div');
    bar.id = 'stale-session-banner';
    bar.className = 'alert alert-warning d-none mb-0 rounded-0';
    bar.style.borderRadius = '0';
    bar.innerHTML = `
        <div class="container-fluid d-flex align-items-center gap-3">
            <strong>Unfinished session detected</strong>
            <span id="stale-session-summary" class="flex-grow-1 small"></span>
            <button id="btn-discard-stale-session" class="btn btn-sm btn-warning"
                    onclick="discardStaleSession()">Mark as ended</button>
            <button class="btn btn-sm btn-link"
                    onclick="dismissStaleSessionBanner()">Later</button>
        </div>`;
    document.body.insertBefore(bar, document.body.firstChild);
    return bar;
}

let _stalePrimary = null;

function showStaleSessionBanner(payload) {
    const bar = _ensureStaleSessionBanner();
    _stalePrimary = (payload && payload.primary) || null;
    if (!_stalePrimary) {
        bar.classList.add('d-none');
        return;
    }
    const p = _stalePrimary;
    const more = (payload.count > 1)
        ? ` <span class="text-muted">(+${payload.count - 1} older — discard one at a time)</span>`
        : '';
    document.getElementById('stale-session-summary').innerHTML =
        `Session #${p.id} (${p.scan_count} scan${p.scan_count === 1 ? '' : 's'}, `
        + `started ${p.start_time})${more}`;
    bar.classList.remove('d-none');
    addLog(`Found ${payload.count} unfinished session(s); most recent #${p.id}`);
}

function dismissStaleSessionBanner() {
    const bar = document.getElementById('stale-session-banner');
    if (bar) bar.classList.add('d-none');
}

async function discardStaleSession() {
    if (!_stalePrimary) return;
    const id = _stalePrimary.id;
    const r = await apiPost('/api/session/discard-stale', { session_id: id });
    // Server emits stale_session_discarded on success; that handler hides
    // the banner. If it didn't fire (network error etc.), apiPost already
    // surfaced the toast — just leave the banner up so the user can retry.
    return r;
}

function onStaleSessionDiscarded(data) {
    addLog(`Marked stale session #${data.session_id} as ended.`);
    // Re-fetch any remaining stale sessions in case there were multiple.
    // The server only emitted the primary in stale_session_detected; the
    // simplest re-check is to ask the server. For now, just hide the
    // banner — the user can refresh to see remaining stale sessions on
    // the next reload.
    dismissStaleSessionBanner();
    _stalePrimary = null;
}

// =========================================================================
// Bin-full prompt (autonomy ladder: "machine pauses only when it has to")
// =========================================================================
// Fired by web_worker when the routed logical bin's whole overflow chain
// is full. Session pauses, banner appears, user empties a bin via the
// existing per-bin Empty button (or refills source / changes config),
// then clicks Resume.

function _ensureBinFullBanner() {
    let bar = document.getElementById('bin-full-banner');
    if (bar) return bar;
    bar = document.createElement('div');
    bar.id = 'bin-full-banner';
    bar.className = 'alert alert-danger d-none mb-0 rounded-0';
    bar.style.borderRadius = '0';
    bar.innerHTML = `
        <div class="container-fluid d-flex align-items-center gap-3">
            <strong>Bin full — session paused</strong>
            <span id="bin-full-summary" class="flex-grow-1 small"></span>
            <button class="btn btn-sm btn-success"
                    onclick="apiPost('/api/session/resume')">Resume</button>
            <button class="btn btn-sm btn-link"
                    onclick="dismissBinFullBanner()">Dismiss</button>
        </div>`;
    document.body.insertBefore(bar, document.body.firstChild);
    return bar;
}

function showBinFullBanner(payload) {
    const bar = _ensureBinFullBanner();
    if (!payload) {
        bar.classList.add('d-none');
        return;
    }
    const chain = (payload.chain || []).join(' → ');
    const dropped = payload.last_dropped !== null && payload.last_dropped !== undefined
        ? `Last card dropped in bin ${payload.last_dropped}.`
        : '';
    document.getElementById('bin-full-summary').textContent =
        `Bin chain ${chain} is full. ${dropped} `
        + `Empty a bin (per-bin Empty button), then Resume.`;
    bar.classList.remove('d-none');
    addLog(`Bin chain ${chain} full — session paused.`);
}

function dismissBinFullBanner() {
    const bar = document.getElementById('bin-full-banner');
    if (bar) bar.classList.add('d-none');
}

// When a bin is marked empty via the existing per-bin button, the
// backend clears it from bins_full. The banner can stay visible until
// the user clicks Resume — keeping it up reminds them they still need
// to take that action.
function onBinFullCleared() {
    dismissBinFullBanner();
}

// =========================================================================
// Source-bin stack estimate
// =========================================================================
// Driven by:
//  - on Sort-tab show: GET /api/source-bin/state (initial paint from
//    cached probe height, if any)
//  - source_bin_count_update socket events: updated whenever the
//    source bin is probed during pickup
//  - source_bin_calibrated socket event: refresh the panel with the
//    fresh empty-Z reference

const _STACK_PANEL_PLACEHOLDER = `
    <p class="text-muted small mb-0">
        No data yet &mdash; pick a card or calibrate the empty source
        bin to start the count.
    </p>`;

function _renderStackEstimate(data) {
    const panel = document.getElementById('stack-estimate-panel');
    if (!panel) return;
    if (!data) {
        panel.innerHTML = _STACK_PANEL_PLACEHOLDER;
        return;
    }
    const calibrated = !!data.calibrated;
    const probedZ = data.probed_z;
    const emptyZ = data.empty_z;
    const count = data.estimated_count;

    if (!calibrated) {
        panel.innerHTML = `
            <p class="small mb-1"><strong>Not calibrated yet.</strong></p>
            <p class="text-muted small mb-0">
                Empty the source bin, then click <em>Calibrate empty</em>
                above. After that the count updates on every pickup.
            </p>`;
        return;
    }
    if (count === null || count === undefined || probedZ === null || probedZ === undefined) {
        panel.innerHTML = `
            <p class="small mb-1">Calibrated, no probe yet.</p>
            <p class="text-muted small mb-0">
                Pick a card from the source bin to populate the estimate.
            </p>`;
        return;
    }
    // Render: big number + a tiny progress-style bar that visualizes
    // delta / a reasonable upper bound (use 300 as a soft full mark
    // since that's our default working capacity per config.DEFAULT_BIN_CAPACITY).
    const softMax = 300;
    const pct = Math.min(100, Math.max(0, Math.round((count / softMax) * 100)));
    panel.innerHTML = `
        <div class="d-flex justify-content-between align-items-baseline mb-1">
            <span class="fw-semibold" style="font-size:1.4rem">~${count}</span>
            <span class="text-muted small">card${count === 1 ? '' : 's'} remaining</span>
        </div>
        <div class="progress" style="height:6px;" title="Estimated stack height vs. ${softMax}-card soft max">
            <div class="progress-bar" role="progressbar"
                 style="width:${pct}%; background-color: var(--accent-blue);"></div>
        </div>
        <p class="text-muted small mb-0 mt-1">
            Probe Z = ${probedZ.toFixed(1)} mm · empty Z = ${emptyZ.toFixed(1)} mm
        </p>`;
}

async function _refreshStackEstimateFromApi() {
    try {
        const data = await apiGet('/api/source-bin/state');
        // apiGet returns the raw JSON; guard against {error} responses.
        if (data && typeof data === 'object' && !data.error) {
            _renderStackEstimate(data);
        }
    } catch (_) {
        /* keep placeholder */
    }
}

function onSourceBinCountUpdate(data) {
    _renderStackEstimate(data);
}

function onSourceBinCalibrated() {
    // Calibration completed — re-fetch the state for a fresh render.
    _refreshStackEstimateFromApi();
}

// =========================================================================
// Hardware self-test (Setup tab)
// =========================================================================

function runSelfTest() {
    const row = document.getElementById('self-test-row');
    const list = document.getElementById('self-test-results');
    const summary = document.getElementById('self-test-summary');
    const btn = document.getElementById('btn-run-self-test');
    if (row) row.style.display = '';
    if (list) list.innerHTML = '<li class="text-muted">Running diagnostics&hellip;</li>';
    if (summary) summary.textContent = '';
    if (btn) btn.disabled = true;
    apiPost('/api/self-test/run');
}

function onSelfTestStarted() {
    const list = document.getElementById('self-test-results');
    if (list) list.innerHTML = '';
}

function onSelfTestStep(data) {
    const list = document.getElementById('self-test-results');
    if (!list) return;
    const colors = { pass: 'success', fail: 'danger', skip: 'warning' };
    const icons = { pass: '✓', fail: '✕', skip: '⚠' };
    const cls = colors[data.status] || 'secondary';
    const icon = icons[data.status] || '?';
    const detail = data.detail
        ? ` <span class="text-muted">${escapeHtml(data.detail)}</span>` : '';
    const li = document.createElement('li');
    li.innerHTML =
        `<span class="badge bg-${cls} me-2">${icon} ${data.status}</span>`
        + `<strong>${escapeHtml(data.step)}</strong>${detail}`;
    list.appendChild(li);
}

function onSelfTestComplete(results) {
    const summary = document.getElementById('self-test-summary');
    const btn = document.getElementById('btn-run-self-test');
    if (btn) btn.disabled = false;
    const p = results.pass || 0;
    const f = results.fail || 0;
    const s = results.skip || 0;
    if (summary) {
        const verdict = (f === 0)
            ? `All checks passed (${p}/${p + f + s}).`
            : `${f} failure${f === 1 ? '' : 's'} — ${p} passed, ${s} skipped.`;
        summary.textContent = verdict;
        summary.className = 'small mt-2 mb-0 ' +
            (f === 0 ? 'text-success' : 'text-danger');
    }
    addLog(`Self-test: ${p} passed, ${f} failed, ${s} skipped.`);
}
