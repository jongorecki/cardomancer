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

function updateBinContentsPanel(cardsPerBin, binDetailsData, binFullness) {
    if (!cardsPerBin) return;
    _cachedBinCounts = cardsPerBin;
    if (binDetailsData) _cachedBinDetails = binDetailsData;
    const binsFull = new Set((binFullness && binFullness.bins_full) || []);
    const binCounts = (binFullness && binFullness.bin_card_counts) || {};
    const limit = (binFullness && binFullness.bin_card_limit) || 150;
    const panel = document.getElementById('session-bin-contents');
    let html = '';
    for (const [bin, count] of Object.entries(cardsPerBin).sort((a, b) => parseInt(a[0]) - parseInt(b[0]))) {
        const cards = _cachedBinDetails[bin] || [];
        const isExpanded = panel.querySelector(`#bin-detail-${bin}`)?.style.display !== 'none';
        const isFull = binsFull.has(parseInt(bin));
        const fullBadge = isFull ? ' <span class="badge bg-danger">FULL</span>' : '';
        const countDisplay = binCounts[bin] ? `${binCounts[bin]}/${limit}` : count;
        html += `<div class="bin-section mb-2">
            <div class="d-flex justify-content-between align-items-center bin-header${isFull ? ' text-danger' : ''}"
                 style="cursor:pointer;">
                <strong onclick="toggleBinDetail(${bin})" class="flex-grow-1">Bin ${bin}${fullBadge}</strong>
                <button class="btn btn-outline-success btn-sm py-0 px-1 me-2"
                        onclick="event.stopPropagation(); markBinEmpty(${bin})"
                        title="Mark bin as emptied (resets card count)">Empty</button>
                <span class="badge ${isFull ? 'bg-danger' : 'bg-secondary'}" onclick="toggleBinDetail(${bin})">${countDisplay}</span>
            </div>
            <div id="bin-detail-${bin}" class="bin-card-list" style="display:${isExpanded ? 'block' : 'none'};">`;
        if (cards.length > 0) {
            for (const card of cards) {
                // Scryfall direct image URL for the hover preview.
                // Only wire it up when we have both set + collector
                // number — without those the URL is useless.
                const canPreview = card.set && card.collector_number;
                const imgUrl = canPreview
                    ? _scryfallImageUrl(card.set, card.collector_number, 'normal')
                    : '';
                const hoverAttr = canPreview
                    ? `onmouseenter="_showCardPreview(event, '${imgUrl}', 'left')" onmouseleave="_hideCardPreview()" style="cursor:help"`
                    : '';
                const foilTag = card.is_foil
                    ? ' <span class="badge bg-warning text-dark" style="font-size:0.65em">★</span>'
                    : '';
                const priceTag = card.price && card.price !== 'null' && card.price !== 'N/A'
                    ? `<span class="text-success ms-1">${card.price}</span>`
                    : '';
                html += `<div class="bin-card-entry small text-truncate ps-2 d-flex justify-content-between align-items-center"
                              title="${card.name} (${card.set}/${card.collector_number})" ${hoverAttr}>
                    <span class="text-truncate">
                        <span class="text-muted">#${card.scan_num}</span> ${card.name}${foilTag}
                    </span>
                    <span class="flex-shrink-0 ms-2">
                        <span class="text-muted">${card.set ? card.set.toUpperCase() : ''}</span>
                        ${priceTag}
                    </span>
                </div>`;
            }
        } else {
            html += `<div class="small text-muted ps-2">${count} card${count !== 1 ? 's' : ''}</div>`;
        }
        html += `</div></div>`;
    }
    panel.innerHTML = html || '<p class="text-muted small">No cards sorted yet</p>';
    drawMotionCanvas();
    drawBinLayoutCanvas();
}

function toggleBinDetail(binNum) {
    const el = document.getElementById(`bin-detail-${binNum}`);
    if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function toggleAllBinDetails() {
    const lists = document.querySelectorAll('.bin-card-list');
    const anyHidden = [...lists].some(el => el.style.display === 'none');
    lists.forEach(el => { el.style.display = anyHidden ? 'block' : 'none'; });
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
