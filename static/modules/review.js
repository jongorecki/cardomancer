// review.js — Session review queue + detection review
// =========================================================================
// Scan Confidence Review Queue
// =========================================================================

let _reviewQueueItems = [];
let _reviewSortKey = 'hash_distance';
let _reviewSortDir = 'desc';  // 'asc' or 'desc'

async function loadReviewQueue() {
    const threshold = document.getElementById('review-threshold')?.value || 12;
    try {
        const data = await apiGet(`/api/collection/review-queue?threshold=${threshold}&limit=100`);
        _reviewQueueItems = data.items || [];
        document.getElementById('review-queue-count').textContent = _reviewQueueItems.length;
        _renderReviewQueue();
    } catch (e) {}
}

function _sortReviewItems(items) {
    const key = _reviewSortKey;
    const dir = _reviewSortDir === 'asc' ? 1 : -1;
    const sorted = [...items];
    sorted.sort((a, b) => {
        let av = a[key];
        let bv = b[key];
        // Nulls/missing go to bottom regardless of direction
        if (av === null || av === undefined || av === '') return 1;
        if (bv === null || bv === undefined || bv === '') return -1;
        // Numeric compare for known numeric keys
        if (key === 'hash_distance' || key === 'session_id' ||
            key === 'scan_num' || key === 'bin') {
            return ((+av) - (+bv)) * dir;
        }
        // String compare otherwise
        av = String(av).toLowerCase();
        bv = String(bv).toLowerCase();
        if (av < bv) return -1 * dir;
        if (av > bv) return 1 * dir;
        return 0;
    });
    return sorted;
}

function setReviewSort(key) {
    if (_reviewSortKey === key) {
        _reviewSortDir = _reviewSortDir === 'asc' ? 'desc' : 'asc';
    } else {
        _reviewSortKey = key;
        // Sensible default: numeric desc, text asc
        _reviewSortDir = (key === 'hash_distance' || key === 'session_id' ||
                          key === 'scan_num' || key === 'bin') ? 'desc' : 'asc';
    }
    _renderReviewQueue();
}

function _renderReviewQueue() {
    const tbody = document.getElementById('review-queue-body');
    const emptyMsg = document.getElementById('review-queue-empty');
    const items = _sortReviewItems(_reviewQueueItems);

    // Update sort arrows on headers
    document.querySelectorAll('#review-queue-table .review-sort').forEach(th => {
        const arrow = th.querySelector('.sort-arrow');
        if (!arrow) return;
        if (th.dataset.sortKey === _reviewSortKey) {
            arrow.textContent = _reviewSortDir === 'asc' ? ' ▲' : ' ▼';
            th.classList.add('text-info');
        } else {
            arrow.textContent = '';
            th.classList.remove('text-info');
        }
    });

    if (items.length === 0) {
        tbody.innerHTML = '';
        emptyMsg.style.display = '';
        return;
    }
    emptyMsg.style.display = 'none';

    tbody.innerHTML = '';
    for (const item of items) {
        const distStr = item.hash_distance !== null ? item.hash_distance.toFixed(1) : 'N/A';
        const distClass = !item.recognized ? 'text-danger' :
            item.hash_distance >= 15 ? 'text-danger' :
            item.hash_distance >= 12 ? 'text-warning' : '';
        const nameDisplay = item.recognized
            ? item.name
            : '<span class="text-danger">UNRECOGNIZED</span>';
        const setDisplay = item.set_code ? item.set_code.toUpperCase() : '';

        // Build Scryfall image URL for the system's guess
        const refImgUrl = item.set_code && item.collector_number
            ? _scryfallImageUrl(item.set_code, item.collector_number, 'normal') : '';
        const hoverAttr = refImgUrl
            ? `onmouseenter="_showCardPreview(event, '${refImgUrl}')" onmouseleave="_hideCardPreview()"` : '';

        // Full camera scan thumbnail
        const scanImg = item.scan_url
            ? `<img src="${item.scan_url}" alt="scan" style="width:60px;height:45px;object-fit:cover;border-radius:3px;cursor:pointer" onclick="_showCardPreview(event, '${item.scan_url}')" onmouseenter="_showCardPreview(event, '${item.scan_url}')" onmouseleave="_hideCardPreview()">`
            : '<span class="text-muted small">N/A</span>';

        // Card crop thumbnail
        const cropImg = item.crop_url
            ? `<img src="${item.crop_url}" alt="crop" style="width:50px;height:70px;object-fit:cover;border-radius:3px;cursor:pointer" onclick="_showCardPreview(event, '${item.crop_url}')" onmouseenter="_showCardPreview(event, '${item.crop_url}')" onmouseleave="_hideCardPreview()">`
            : '<span class="text-muted small">N/A</span>';

        // Reference (system guess) thumbnail — only when recognized
        const guessImg = (item.recognized && refImgUrl)
            ? `<img src="${refImgUrl}" alt="guess" style="width:50px;height:70px;object-fit:cover;border-radius:3px;cursor:pointer" onclick="_showCardPreview(event, '${refImgUrl}')" onmouseenter="_showCardPreview(event, '${refImgUrl}')" onmouseleave="_hideCardPreview()">`
            : '<span class="text-muted small">—</span>';

        const diagBtn = item.diagnostics_url
            ? `<button class="btn btn-outline-info btn-sm py-0 px-1" onclick="showHashDiagnostics('${item.diagnostics_url}')" title="Hash diagnostics">&#128269;</button>`
            : '';

        tbody.innerHTML += `<tr>
            <td>${scanImg}</td>
            <td>${cropImg}</td>
            <td>${guessImg}</td>
            <td class="small">#${item.session_id}</td>
            <td>${item.scan_num}</td>
            <td class="card-hover-name" style="cursor:default" ${hoverAttr}>${nameDisplay}</td>
            <td>${setDisplay}</td>
            <td class="small">${item.method || ''}</td>
            <td class="${distClass}"><strong>${distStr}</strong></td>
            <td>${item.bin !== null ? item.bin : ''}</td>
            <td class="text-nowrap">
                ${item.recognized ? `<button class="btn btn-outline-success btn-sm py-0 px-1" onclick="confirmReviewItem(${item.id})" title="Confirm correct">&#10003;</button>` : ''}
                <button class="btn btn-outline-primary btn-sm py-0 px-1" onclick="showCorrectDialog(${item.id}, '${(item.name || '').replace(/'/g, "\\'")}', '${item.crop_url || ''}')" title="Correct identification">&#9998;</button>
                ${diagBtn}
                <button class="btn btn-outline-secondary btn-sm py-0 px-1" onclick="dismissReviewItem(${item.id})" title="Dismiss">&times;</button>
            </td>
        </tr>`;
    }
}

// Wire up sortable column headers (delegated, runs once on first load)
document.addEventListener('click', (e) => {
    const th = e.target.closest('#review-queue-table .review-sort');
    if (th && th.dataset.sortKey) {
        setReviewSort(th.dataset.sortKey);
    }
});

async function confirmReviewItem(scanId) {
    await apiPost(`/api/collection/review-queue/${scanId}/confirm`);
    loadReviewQueue();
}

async function dismissReviewItem(scanId) {
    await apiPost(`/api/collection/review-queue/${scanId}/dismiss`);
    loadReviewQueue();
}

function showCorrectDialog(scanId, currentName, cropUrl) {
    const overlay = document.createElement('div');
    overlay.id = 'review-correct-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;z-index:9999';

    const cropHtml = cropUrl
        ? `<img src="${cropUrl}" alt="Scanned card" style="max-width:200px;max-height:280px;border-radius:6px;border:2px solid #555">`
        : '<div class="text-muted small p-4" style="border:1px dashed #555;border-radius:6px">No crop image available</div>';

    overlay.innerHTML = `
        <div style="background:var(--bg-card,#1c1f26);border:1px solid var(--border-subtle,#363a45);border-radius:8px;padding:20px;min-width:700px;max-width:800px;color:var(--text-primary,#e8eaf0)">
            <h6>Correct Card Identification</h6>
            <p class="small text-secondary mb-3">Current: ${currentName || 'UNRECOGNIZED'}</p>
            <div class="d-flex gap-3">
                <div class="text-center" style="flex:0 0 210px">
                    <div class="small text-secondary mb-1">Scanned Card</div>
                    ${cropHtml}
                </div>
                <div style="flex:1;min-width:0">
                    <div class="position-relative mb-2">
                        <input type="text" class="form-control form-control-sm" id="correct-card-name"
                               placeholder="Correct card name *" autocomplete="off" value="${currentName || ''}">
                        <div id="correct-card-suggestions" class="list-group position-absolute w-100"
                             style="z-index:1060;max-height:200px;overflow-y:auto;display:none"></div>
                    </div>
                    <select class="form-select form-select-sm mb-2" id="correct-card-set" disabled onchange="_updateConfirmImage()">
                        <option value="">Select a card first</option>
                    </select>
                    <div id="correct-card-info" class="small text-secondary mb-2" style="display:none"></div>
                    <div class="d-flex gap-2">
                        <button class="btn btn-primary btn-sm" id="correct-confirm-btn" onclick="submitCorrection(${scanId})">Save Correction</button>
                        <button class="btn btn-outline-secondary btn-sm" onclick="document.getElementById('review-correct-overlay').remove()">Cancel</button>
                    </div>
                </div>
                <div class="text-center" id="correct-confirm-image-container" style="flex:0 0 210px">
                    <div class="small text-secondary mb-1">Correct Card</div>
                    <div id="correct-confirm-image" class="text-muted small p-4" style="border:1px dashed #555;border-radius:6px">
                        Select a card to see its image
                    </div>
                </div>
            </div>
        </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

    // Set up autocomplete for the correction dialog
    const input = document.getElementById('correct-card-name');
    const sugBox = document.getElementById('correct-card-suggestions');
    let debounce = null;

    input.addEventListener('input', () => {
        clearTimeout(debounce);
        const q = input.value.trim();
        if (q.length < 2) { sugBox.style.display = 'none'; return; }
        debounce = setTimeout(async () => {
            try {
                const data = await apiGet(`/api/cards/autocomplete?q=${encodeURIComponent(q)}`);
                if (!data.suggestions || data.suggestions.length === 0) {
                    sugBox.style.display = 'none';
                    return;
                }
                sugBox.innerHTML = data.suggestions.map(name =>
                    `<button type="button" class="list-group-item list-group-item-action py-1 px-2 small"
                        onclick="_selectCorrectCardName('${name.replace(/'/g, "\\'")}')">${name}</button>`
                ).join('');
                sugBox.style.display = 'block';
            } catch (e) { sugBox.style.display = 'none'; }
        }, 250);
    });

    input.focus();
    input.select();
}

let _correctCardPrintings = [];

async function _selectCorrectCardName(name) {
    document.getElementById('correct-card-name').value = name;
    document.getElementById('correct-card-suggestions').style.display = 'none';
    const setSelect = document.getElementById('correct-card-set');

    try {
        const data = await apiGet(`/api/cards/printings?name=${encodeURIComponent(name)}`);
        _correctCardPrintings = data.printings || [];
        setSelect.innerHTML = '';
        if (_correctCardPrintings.length === 0) {
            setSelect.innerHTML = '<option value="">No printings found</option>';
            setSelect.disabled = true;
        } else {
            for (const p of _correctCardPrintings) {
                const priceStr = p.price_usd ? ` — $${p.price_usd.toFixed(2)}` : '';
                const opt = document.createElement('option');
                opt.value = `${p.set}|${p.collector_number}`;
                opt.textContent = `${p.set.toUpperCase()} #${p.collector_number} (${p.rarity})${priceStr}`;
                setSelect.appendChild(opt);
            }
            setSelect.disabled = false;
            // Show the first printing's image immediately
            _updateConfirmImage();
        }
    } catch (e) {
        setSelect.innerHTML = '<option value="">Error</option>';
        setSelect.disabled = true;
    }
}

async function showHashDiagnostics(url) {
    try {
        const resp = await fetch(url);
        if (!resp.ok) { alert('Diagnostics not available'); return; }
        const diag = await resp.json();

        const overlay = document.createElement('div');
        overlay.id = 'hash-diag-overlay';
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;z-index:9999;overflow-y:auto';

        let html = `<div style="background:var(--bg-card,#1c1f26);border:1px solid var(--border-subtle,#363a45);border-radius:8px;padding:20px;min-width:500px;max-width:700px;color:var(--text-primary,#e8eaf0);max-height:90vh;overflow-y:auto">
            <h6>Hash Diagnostics — Scan #${diag.scan_num}</h6>
            <p class="small text-secondary mb-2">Layout detected: <strong>${diag.layout}</strong></p>
            <p class="small text-secondary mb-3">Top ${diag.candidates.length} candidates (per-channel distances):</p>`;

        for (const c of diag.candidates) {
            const ph = c.phash;
            const dh = c.dhash;
            const phAvg = ((ph.r + ph.g + ph.b) / 3).toFixed(1);
            const dhAvg = ((dh.r + dh.g + dh.b) / 3).toFixed(1);
            const combined = c.combined_distance.toFixed(1);
            const distColor = c.combined_distance <= 10 ? 'text-success' :
                              c.combined_distance <= 15 ? 'text-warning' : 'text-danger';

            html += `<div class="mb-3 p-2" style="border:1px solid #363a45;border-radius:6px">
                <div class="d-flex justify-content-between align-items-start">
                    <div>
                        <strong>${c.name}</strong>
                        <span class="text-secondary small">[${c.set.toUpperCase()}]</span>
                        <span class="text-secondary small ms-1">layout: ${c.db_layout}</span>
                    </div>
                    <span class="${distColor}"><strong>${combined}</strong></span>
                </div>
                <table class="table table-sm table-bordered mt-1 mb-0" style="font-size:0.8em">
                    <thead><tr><th></th><th class="text-danger">R</th><th class="text-success">G</th><th class="text-primary">B</th><th>Avg</th></tr></thead>
                    <tbody>
                        <tr><td><strong>phash</strong></td>
                            <td>${ph.r}</td><td>${ph.g}</td><td>${ph.b}</td>
                            <td><strong>${phAvg}</strong></td></tr>
                        <tr><td><strong>dhash</strong></td>
                            <td>${dh.r}</td><td>${dh.g}</td><td>${dh.b}</td>
                            <td><strong>${dhAvg}</strong></td></tr>
                    </tbody>
                </table>
            </div>`;
        }

        html += `<div class="text-end"><button class="btn btn-outline-secondary btn-sm" onclick="document.getElementById('hash-diag-overlay').remove()">Close</button></div></div>`;
        overlay.innerHTML = html;
        document.body.appendChild(overlay);
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    } catch (e) {
        alert('Error loading diagnostics: ' + e.message);
    }
}

function _updateConfirmImage() {
    const setSelect = document.getElementById('correct-card-set');
    const container = document.getElementById('correct-confirm-image');
    if (!setSelect || !container) return;

    const val = setSelect.value;
    if (!val) {
        container.innerHTML = '<span class="text-muted small">Select a card to see its image</span>';
        return;
    }
    const [set_code, collector_number] = val.split('|');
    if (!set_code || !collector_number) return;

    const imgUrl = _scryfallImageUrl(set_code, collector_number, 'normal');
    container.innerHTML = `<img src="${imgUrl}" alt="Correct card" style="max-width:200px;max-height:280px;border-radius:6px;border:2px solid #0d6efd" onerror="this.parentElement.innerHTML='<span class=\\'text-muted small\\'>Image not available</span>'">`;
}

async function submitCorrection(scanId) {
    const name = document.getElementById('correct-card-name').value.trim();
    const setVal = document.getElementById('correct-card-set').value;
    if (!name) { alert('Enter a card name'); return; }

    const [set_code, collector_number] = setVal ? setVal.split('|') : ['', ''];
    const printing = _correctCardPrintings.find(
        p => p.set === set_code && p.collector_number === collector_number);

    try {
        const resp = await apiPost(`/api/collection/review-queue/${scanId}/correct`, {
            name,
            set_code: set_code || '',
            collector_number: collector_number || '',
            colors: printing?.colors || null,
            type_line: printing?.type_line || null,
            rarity: printing?.rarity || null,
            price_usd: printing?.price_usd || null,
        });
        if (resp.corrected) {
            addLog(`Corrected scan: ${resp.old?.name || 'unknown'} -> ${resp.new?.name}`);
            document.getElementById('review-correct-overlay')?.remove();
            loadReviewQueue();
            loadInventory(_inventoryPage);
            loadCollectionStats();
        } else {
            alert(resp.error || 'Correction failed');
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

// =========================================================================
// Per-attribute Detection Review Queues (Session Review tab)
// =========================================================================

let _currentDetectionVariable = 'foil';

function selectDetectionReview(variable) {
    _currentDetectionVariable = variable;
    document.querySelectorAll('#detection-review-subnav .nav-link').forEach(el => {
        el.classList.toggle('active',
            el.getAttribute('data-review-variable') === variable);
    });
    loadDetectionReviewQueue();
    return false;
}

async function loadDetectionReviewCounts() {
    try {
        const resp = await fetch('/api/detection-reviews/counts');
        if (!resp.ok) return;
        const counts = await resp.json();
        let total = 0;
        for (const variable of ['foil', 'border', 'set_symbol']) {
            const pending = (counts[variable] && counts[variable].pending) || 0;
            total += pending;
            const badge = document.getElementById('detection-badge-' + variable);
            if (badge) badge.textContent = pending;
        }
        const totalBadge = document.getElementById('detection-review-total-badge');
        if (totalBadge) {
            totalBadge.textContent = total;
            totalBadge.style.display = total > 0 ? '' : 'none';
        }
        // If Setup tab is currently visible, refresh the active queue
        // (Session Review folded into Setup post-Phase-1b).
        const pane = document.getElementById('tab-setup');
        if (pane && pane.classList.contains('active')) {
            loadDetectionReviewQueue();
        }
    } catch (e) {
        console.error('loadDetectionReviewCounts failed', e);
    }
}

async function seedDetectionReviews() {
    try {
        await fetch('/api/detection-reviews/seed', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({only_recognized: true}),
        });
    } catch (e) {
        console.error('seedDetectionReviews failed', e);
    }
    await loadDetectionReviewCounts();
    await loadDetectionReviewQueue();
}

function _detectionConfidenceLabel(r) {
    if (r.confidence === null || r.confidence === undefined) return 'unknown';
    try {
        return Number(r.confidence).toFixed(3);
    } catch (e) {
        return String(r.confidence);
    }
}

function _detectionVerdictBadge(r) {
    if (!r.verdict) return '';
    const colors = {correct: 'success', wrong: 'danger', skip: 'secondary'};
    const color = colors[r.verdict] || 'info';
    return `<span class="badge bg-${color} ms-1">${r.verdict}</span>`;
}

async function loadDetectionReviewQueue() {
    const variable = _currentDetectionVariable;
    const includeReviewed = document.getElementById('detection-show-reviewed');
    const include = includeReviewed && includeReviewed.checked ? '1' : '0';
    const body = document.getElementById('detection-review-body');
    const empty = document.getElementById('detection-review-empty');
    if (!body) return;
    body.innerHTML = '<p class="text-muted small">Loading…</p>';
    try {
        const resp = await fetch(
            `/api/detection-reviews/${encodeURIComponent(variable)}`
            + `?include_reviewed=${include}&limit=200`);
        if (!resp.ok) {
            body.innerHTML = `<p class="text-danger">Failed to load queue.</p>`;
            return;
        }
        const data = await resp.json();
        const items = data.items || [];
        if (!items.length) {
            body.innerHTML = '';
            if (empty) {
                empty.style.display = '';
                body.appendChild(empty);
            } else {
                body.innerHTML = '<p class="text-muted text-center py-4">Nothing to review.</p>';
            }
            return;
        }

        const rows = items.map(r => {
            const crop = r.crop_url
                ? `<img src="${r.crop_url}" style="max-height:140px;max-width:110px" class="rounded border">`
                : `<div class="text-muted small">no crop</div>`;
            const card = r.name
                ? `<div><strong>${_escapeHtml(r.name)}</strong></div>`
                  + `<div class="text-muted small">${_escapeHtml(r.set_code || '')} ${_escapeHtml(r.collector_number || '')}</div>`
                : `<div class="text-muted">unknown card</div>`;
            const detected = r.detected_value
                ? `<code>${_escapeHtml(String(r.detected_value))}</code>`
                : `<span class="text-muted">—</span>`;
            const conf = _detectionConfidenceLabel(r);
            const verdictBadge = _detectionVerdictBadge(r);
            const correctionInput = r.verdict
                ? ''
                : `<input type="text" class="form-control form-control-sm mt-1 d-none"
                          id="correction-${r.id}" placeholder="correct value (optional)">`;
            const actions = r.verdict
                ? `<span class="text-muted small">reviewed ${r.reviewed_at || ''}</span>`
                : `
                  <button class="btn btn-success btn-sm"
                          onclick="markDetectionVerdict(${r.id}, 'correct')">Correct</button>
                  <button class="btn btn-danger btn-sm"
                          onclick="markDetectionVerdict(${r.id}, 'wrong')">Wrong</button>
                  <button class="btn btn-outline-secondary btn-sm"
                          onclick="markDetectionVerdict(${r.id}, 'skip')">Skip</button>
                `;
            return `
              <tr data-review-id="${r.id}">
                <td style="width:130px">${crop}</td>
                <td>${card}</td>
                <td>${detected}</td>
                <td><span class="badge bg-info">${conf}</span></td>
                <td>${verdictBadge}</td>
                <td style="min-width:260px">${actions}${correctionInput}</td>
              </tr>`;
        }).join('');

        body.innerHTML = `
          <table class="table table-sm align-middle">
            <thead><tr>
              <th>Crop</th><th>Card</th><th>Detected</th>
              <th>Confidence</th><th>Verdict</th><th>Actions</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
    } catch (e) {
        console.error('loadDetectionReviewQueue failed', e);
        body.innerHTML = '<p class="text-danger">Error loading queue.</p>';
    }
}

async function markDetectionVerdict(reviewId, verdict) {
    let correction = null;
    if (verdict === 'wrong') {
        const input = document.getElementById('correction-' + reviewId);
        if (input) {
            input.classList.remove('d-none');
            correction = input.value || null;
        }
    }
    try {
        await fetch(`/api/detection-reviews/${reviewId}/verdict`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({verdict, correction}),
        });
    } catch (e) {
        console.error('markDetectionVerdict failed', e);
        return;
    }
    await loadDetectionReviewCounts();
    await loadDetectionReviewQueue();
}
