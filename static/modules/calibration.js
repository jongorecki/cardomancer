// calibration.js — Calibration / hardware setup / drop tuner / aruco
// =========================================================================
// ArUco Calibration
// =========================================================================

async function startCalibration() {
    const sources = parseInt(document.getElementById('cal-expected-sources').value) || 1;
    const dests = parseInt(document.getElementById('cal-expected-dests').value) || 10;
    const maxSweep = parseFloat(document.getElementById('cal-max-sweep-x').value) || 790;
    const expectStaging = document.getElementById('cal-expect-staging').checked;

    // Await the server's acceptance. If the server returns 409 (not
    // connected / busy / estopped), apiPost will already have surfaced
    // the error — we just need to NOT flip the UI into "running"
    // state so the user can click again after fixing the issue.
    const resp = await apiPost('/api/calibration/start', {
        expected_sources: sources,
        expected_dests: dests,
        expect_staging: expectStaging,
        max_sweep_x: maxSweep,
    });
    if (!resp || !resp.ok) return;

    document.getElementById('btn-start-cal').disabled = true;
    document.getElementById('btn-cancel-cal').style.display = '';
    document.getElementById('cal-progress-container').style.display = '';
    document.getElementById('cal-discovered-bins').innerHTML = '';
    const rejEl = document.getElementById('cal-rejected-markers');
    if (rejEl) rejEl.innerHTML = '';
    _calLockedMarkers = {};
}

async function cancelCalibration() {
    await apiPost('/api/calibration/cancel');
    // Immediately re-enable controls locally — the server's cancel
    // is synchronous (it sets the calibrator flag directly and the
    // worker's try/finally will reset state to idle), but there can
    // be a small delay before hardware_setup_complete arrives.
    // Re-enabling here gives instant feedback.
    const hwBtn = document.getElementById('btn-new-hw-setup');
    if (hwBtn) hwBtn.disabled = false;
    document.getElementById('btn-start-cal').disabled = false;
    document.getElementById('btn-cancel-cal').style.display = 'none';
    const msg = document.getElementById('cal-progress-message');
    if (msg) msg.textContent = 'Cancelling...';
}

// --- Live ArUco camera view ---
let arucoLiveActive = false;

function toggleArucoLive() {
    const img = document.getElementById('aruco-live-feed');
    const placeholder = document.getElementById('aruco-live-placeholder');
    const status = document.getElementById('aruco-live-status');
    const btn = document.getElementById('btn-aruco-live-toggle');

    if (!arucoLiveActive) {
        img.src = '/api/camera/feed-aruco?t=' + Date.now();
        img.style.display = '';
        if (placeholder) placeholder.style.display = 'none';
        status.textContent = 'Live';
        status.className = 'small text-success me-2';
        btn.textContent = 'Stop Live View';
        btn.className = 'btn btn-sm btn-outline-danger';
        arucoLiveActive = true;
    } else {
        img.src = '';
        img.style.display = 'none';
        if (placeholder) placeholder.style.display = '';
        status.textContent = 'Off';
        status.className = 'small text-secondary me-2';
        btn.textContent = 'Start Live View';
        btn.className = 'btn btn-sm btn-outline-primary';
        arucoLiveActive = false;
    }
}

// Auto-start the live ArUco feed when the Calibration tab is activated,
// and stop it when navigating away, so we don't leave a hidden MJPEG
// stream running in the background.
document.addEventListener('DOMContentLoaded', () => {
    // Calibration content folded into the Setup tab post-Phase-1b.
    // Note: the ArUco live MJPEG stream now starts whenever Setup is
    // shown — even if the user only opened Setup to view Hardware. A
    // future polish pass should gate this on the calibration sub-section
    // actually being scrolled into view.
    const calTab = document.querySelector('a[href="#tab-setup"]');
    if (calTab) {
        calTab.addEventListener('shown.bs.tab', () => {
            if (!arucoLiveActive) toggleArucoLive();
            refreshSavedSetups();
        });
        calTab.addEventListener('hidden.bs.tab', () => {
            if (arucoLiveActive) toggleArucoLive();
        });
    }
    // Also populate it once at page load so it's ready if the user's
    // default tab happens to be Calibration.
    refreshSavedSetups();
});

async function newHardwareSetup() {
    const sources = parseInt(document.getElementById('cal-expected-sources').value) || 1;
    const dests = parseInt(document.getElementById('cal-expected-dests').value) || 10;
    const maxSweep = parseFloat(document.getElementById('cal-max-sweep-x').value) || 790;
    const expectStaging = document.getElementById('cal-expect-staging').checked;

    const msg =
        'NEW HARDWARE SETUP will:\n\n' +
        '  1. Clear ALL cached state (probe cache, bin positions, fullness counts)\n' +
        '  2. Home all axes\n' +
        '  3. Run ArUco calibration sweep\n' +
        `  4. Probe Z at every destination bin (${dests} bins)\n\n` +
        'Make sure the machine is connected, the source/destination bins are ' +
        'installed with their ArUco markers, and nothing is in the way of the ' +
        'X carriage.\n\nContinue?';
    if (!confirm(msg)) return;

    // Wait for the server to accept the command. If it rejects us
    // (e.g. machine not connected), apiPost shows the error — we
    // must NOT disable the buttons in that case, otherwise the user
    // is stuck with a greyed-out UI until they reload the page.
    const resp = await apiPost('/api/calibration/new-hardware-setup', {
        expected_sources: sources,
        expected_dests: dests,
        expect_staging: expectStaging,
        max_sweep_x: maxSweep,
    });
    if (!resp || !resp.ok) return;

    const btn = document.getElementById('btn-new-hw-setup');
    if (btn) btn.disabled = true;
    document.getElementById('btn-start-cal').disabled = true;
    document.getElementById('btn-cancel-cal').style.display = '';
    document.getElementById('cal-progress-container').style.display = '';
    document.getElementById('cal-discovered-bins').innerHTML = '';
    const rejEl2 = document.getElementById('cal-rejected-markers');
    if (rejEl2) rejEl2.innerHTML = '';
    _calLockedMarkers = {};
    const progressMsg = document.getElementById('cal-progress-message');
    if (progressMsg) progressMsg.textContent = 'Starting new hardware setup...';
}

// --- Hardware setup: Retry / Save-as / Reload auto-saved setup ---

async function retryHardwareSetup() {
    // Reuse the current UI values — no confirm prompt this time since
    // the user has already been here and just wants another pass.
    const sources = parseInt(document.getElementById('cal-expected-sources').value) || 1;
    const dests = parseInt(document.getElementById('cal-expected-dests').value) || 10;
    const maxSweep = parseFloat(document.getElementById('cal-max-sweep-x').value) || 790;
    const expectStaging = document.getElementById('cal-expect-staging').checked;

    const resp = await apiPost('/api/calibration/retry', {
        expected_sources: sources,
        expected_dests: dests,
        expect_staging: expectStaging,
        max_sweep_x: maxSweep,
    });
    if (!resp || !resp.ok) return;

    const btn = document.getElementById('btn-new-hw-setup');
    if (btn) btn.disabled = true;
    document.getElementById('btn-retry-hw-setup').disabled = true;
    document.getElementById('btn-start-cal').disabled = true;
    document.getElementById('btn-cancel-cal').style.display = '';
    document.getElementById('cal-progress-container').style.display = '';
    document.getElementById('cal-discovered-bins').innerHTML = '';
    const rejEl = document.getElementById('cal-rejected-markers');
    if (rejEl) rejEl.innerHTML = '';
    _calLockedMarkers = {};
    const progressMsg = document.getElementById('cal-progress-message');
    if (progressMsg) progressMsg.textContent = 'Retrying hardware setup...';
}

async function reloadLastSetup() {
    const resp = await apiPost('/api/calibration/last-setup/reload', {});
    if (!resp || !resp.ok) {
        // apiPost already surfaced the error; nothing else to do.
        return;
    }
    addLog('Reloaded last auto-saved setup from disk');
    loadBinTable();
    loadBinLocations();
    loadSourceBinsStatus();
    const summary = resp.summary || {};
    const msg = document.getElementById('cal-progress-message');
    if (msg) {
        msg.textContent =
            `Reloaded: ${summary.dest_bin_count || 0} dest bins, ` +
            `${summary.source_bin_count || 0} source bins` +
            (summary.staging ? `, staging at X=${summary.staging.x}mm` : '');
    }
}

async function saveSetupAs() {
    const input = document.getElementById('cal-save-name');
    const name = (input?.value || '').trim();
    if (!name) {
        alert('Enter a name for the saved setup first.');
        return;
    }
    const resp = await apiPost('/api/calibration/last-setup/save-as', { name });
    if (!resp || !resp.ok) return;
    addLog(`Saved setup as ${resp.filename}`);
    if (input) input.value = '';
    // Refresh the dropdown so the new file shows up immediately, and
    // pre-select it so the user can see what they just saved.
    await refreshSavedSetups(resp.filename);
}

// Pull the list of saved setup_*.json files from the server and
// repopulate the dropdown. Optionally preselect a filename (used
// right after a save so the new entry is auto-selected).
async function refreshSavedSetups(selectFilename) {
    const sel = document.getElementById('cal-saved-select');
    if (!sel) return;
    let resp;
    try {
        const r = await fetch('/api/calibration/last-setup/list');
        resp = await r.json();
    } catch (e) {
        addLog(`Failed to list saved setups: ${e}`);
        return;
    }
    const setups = (resp && resp.setups) || [];
    // Remember the current selection if no override was provided so
    // that a refresh triggered by something unrelated doesn't stomp
    // the user's pick.
    const keep = selectFilename || sel.value;
    sel.innerHTML = '';
    if (!setups.length) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '(none saved)';
        sel.appendChild(opt);
        return;
    }
    for (const s of setups) {
        const opt = document.createElement('option');
        opt.value = s.filename;
        const countStr =
            (s.dest_bin_count != null ? s.dest_bin_count + 'D' : '') +
            (s.source_bin_count != null ? '/' + s.source_bin_count + 'S' : '');
        opt.textContent = s.error
            ? `${s.name}  [corrupt]`
            : `${s.name}  (${countStr})`;
        if (s.error) opt.disabled = true;
        sel.appendChild(opt);
    }
    // Restore selection if it still exists in the new list.
    if (keep && Array.from(sel.options).some(o => o.value === keep)) {
        sel.value = keep;
    }
}

async function loadSavedSetup() {
    const sel = document.getElementById('cal-saved-select');
    const filename = sel?.value || '';
    if (!filename) {
        alert('Pick a saved setup to load first.');
        return;
    }
    const resp = await apiPost('/api/calibration/last-setup/load', { filename });
    if (!resp || !resp.ok) return;
    const summary = resp.summary || {};
    addLog(`Loaded saved setup ${resp.filename}`);
    loadBinTable();
    loadBinLocations();
    loadSourceBinsStatus();
    const msg = document.getElementById('cal-progress-message');
    if (msg) {
        msg.textContent =
            `Loaded ${summary.name || resp.filename}: ${summary.dest_bin_count || 0} dest bins, ` +
            `${summary.source_bin_count || 0} source bins` +
            (summary.staging ? `, staging at X=${summary.staging.x}mm` : '');
    }
}

async function deleteSavedSetup() {
    const sel = document.getElementById('cal-saved-select');
    const filename = sel?.value || '';
    if (!filename) {
        alert('Pick a saved setup to delete first.');
        return;
    }
    if (!confirm(`Delete saved setup "${filename}"? This cannot be undone.`)) {
        return;
    }
    const resp = await apiPost('/api/calibration/last-setup/delete', { filename });
    if (!resp || !resp.ok) return;
    addLog(`Deleted saved setup ${resp.filename}`);
    await refreshSavedSetups();
}

// Running table of markers that have been locked in during the current
// sweep. Rebuilt from scratch on every sweep start.
let _calLockedMarkers = {};

function renderLockedMarkersTable() {
    const el = document.getElementById('cal-discovered-bins');
    if (!el) return;
    const ids = Object.keys(_calLockedMarkers).map(Number).sort((a, b) => a - b);
    if (ids.length === 0) {
        el.innerHTML = '';
        return;
    }
    let html =
        '<div class="small mt-2"><strong>Locked markers (' + ids.length + '):</strong></div>' +
        '<table class="table table-sm"><thead><tr>' +
        '<th>&#10003;</th><th>ID</th><th>Type</th><th>X (mm)</th><th>Samples</th>' +
        '</tr></thead><tbody>';
    for (const id of ids) {
        const m = _calLockedMarkers[id];
        const typeClass =
            m.type === 'source' ? 'text-info'
            : m.type === 'staging' ? 'text-warning'
            : 'text-success';
        html += `<tr>
            <td class="text-success">&#10003;</td>
            <td>${id}</td>
            <td class="${typeClass}">${(m.type || '').toUpperCase()}</td>
            <td>${m.bin_x}</td>
            <td>${m.sample_count || '-'}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
}

async function detectMarkersNow() {
    try {
        const data = await apiPost('/api/calibration/detect-markers');
        const preview = document.getElementById('cal-marker-preview');
        const img = document.getElementById('cal-marker-preview-img');

        if (data.frame) {
            img.src = data.frame;
            preview.style.display = 'block';
        }

        if (data.markers && data.markers.length > 0) {
            addLog(`Detected ${data.markers.length} markers: ${data.markers.map(m => `ID${m.id}(${m.type})`).join(', ')}`);
        } else {
            addLog('No ArUco markers detected in current frame');
        }
    } catch (e) {
        addLog('Marker detection failed: ' + e.message);
    }
}

async function checkSourceEmpty() {
    await apiPost('/api/calibration/check-empty');
}

async function saveCameraOffset() {
    const offset = parseFloat(document.getElementById('cal-camera-offset').value);
    const data = await apiPost('/api/calibration/offset', { offset });
    if (data.updated) {
        addLog(`Camera X offset set to ${data.offset}mm`);
    }
}

async function loadSourceBinsStatus() {
    try {
        const data = await apiGet('/api/calibration/source-bins');
        const panel = document.getElementById('source-bins-status');
        if (!data.source_bins || data.source_bins.length === 0) {
            panel.innerHTML = '<p class="small text-muted">No calibration data</p>';
            return;
        }
        let html = '';
        for (const s of data.source_bins) {
            const empty = data.source_empty[s.bin_number];
            const statusBadge = empty === true
                ? '<span class="badge bg-danger">EMPTY</span>'
                : empty === false
                    ? '<span class="badge bg-success">Has cards</span>'
                    : '<span class="badge bg-secondary">Unknown</span>';
            html += `<div class="small mb-1">
                Source ${s.bin_number} (marker ${s.marker_id}): X=${s.x}mm ${statusBadge}
            </div>`;
        }
        if (data.multi_source) {
            html += '<div class="small text-info mt-1">Dual-source optimization active</div>';
        }
        panel.innerHTML = html;
    } catch (e) {}
}

async function loadCameraOffset() {
    try {
        const data = await apiGet('/api/calibration/offset');
        document.getElementById('cal-camera-offset').value = data.offset;
    } catch (e) {}
}

// =========================================================================
// Drop-height tuner (Calibration tab > Drop height tuning)
// -------------------------------------------------------------------------
// Drives the standalone tuner state machine in the worker. The buttons
// fire and forget — UI state advances when the worker emits
// drop_tuner_progress / drop_tuner_complete events.
// =========================================================================

let _dropTunerInFlight = false;

function _dropTunerSetButtonsDisabled(disabled) {
    // Disable every step / test / save button while a worker command is
    // in flight so a flurry of clicks doesn't queue a multi-step
    // ladder of moves. Re-enabled on the next progress event.
    const ids = [
        'btn-drop-tuner-pickup',
        'btn-drop-tuner-test',
        'btn-drop-tuner-save',
    ];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = !!disabled;
    });
    document.querySelectorAll('.drop-tuner-step-group button').forEach(btn => {
        btn.disabled = !!disabled;
    });
}

function _dropTunerShowPhase(phase) {
    // 'idle' | 'awaiting_card' | 'tuning'
    const idle = document.getElementById('drop-tuner-idle');
    const active = document.getElementById('drop-tuner-active');
    const awaiting = document.getElementById('drop-tuner-awaiting');
    const tuning = document.getElementById('drop-tuner-tuning');
    if (!idle || !active || !awaiting || !tuning) return;
    if (phase === 'idle' || !phase) {
        idle.style.display = '';
        active.style.display = 'none';
        awaiting.style.display = 'none';
        tuning.style.display = 'none';
    } else {
        idle.style.display = 'none';
        active.style.display = '';
        awaiting.style.display = (phase === 'awaiting_card') ? '' : 'none';
        tuning.style.display = (phase === 'tuning') ? '' : 'none';
    }
}

async function _dropTunerCallApi(url, body) {
    _dropTunerInFlight = true;
    _dropTunerSetButtonsDisabled(true);
    const resp = await apiPost(url, body);
    if (!resp || !resp.ok) {
        // apiPost already showed an error toast — re-enable buttons
        // since we won't get a progress event back.
        _dropTunerInFlight = false;
        _dropTunerSetButtonsDisabled(false);
    }
    return resp;
}

async function dropTunerStart() {
    const resp = await _dropTunerCallApi('/api/calibration/drop-tuner/start');
    if (resp && resp.ok) {
        addLog('Drop tuner: starting...');
        const startBtn = document.getElementById('btn-drop-tuner-start');
        if (startBtn) startBtn.disabled = true;
    }
}

async function dropTunerPickup() {
    addLog('Drop tuner: picking up card...');
    await _dropTunerCallApi('/api/calibration/drop-tuner/pickup');
}

async function dropTunerStep(deltaMm) {
    await _dropTunerCallApi('/api/calibration/drop-tuner/step-z',
                            { delta_mm: deltaMm });
}

async function dropTunerTestDrop() {
    addLog('Drop tuner: test drop...');
    await _dropTunerCallApi('/api/calibration/drop-tuner/test-drop');
}

async function dropTunerSave() {
    addLog('Drop tuner: saving...');
    await _dropTunerCallApi('/api/calibration/drop-tuner/save');
}

async function dropTunerCancel() {
    addLog('Drop tuner: cancelling...');
    await _dropTunerCallApi('/api/calibration/drop-tuner/cancel');
}

// On page load, fetch current Z_DROP_OFFSET so the displayed value is accurate.
document.addEventListener('DOMContentLoaded', async () => {
    try {
        const data = await apiGet('/api/calibration/drop-tuner/status');
        const offEl = document.getElementById('drop-tuner-current-offset');
        if (offEl && data && typeof data.current_offset === 'number') {
            offEl.textContent = Number(data.current_offset).toFixed(1);
        }
    } catch (_) { /* non-fatal */ }
});
