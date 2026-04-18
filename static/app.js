// app.js — MTG Card Sorter Web UI
// SocketIO client, API helpers, motion canvas, and UI logic.

// =========================================================================
// SocketIO connection
// =========================================================================
const socket = io();

socket.on('connect', () => {
    console.log('SocketIO connected');
    addLog('Connected to server');
});

socket.on('disconnect', () => {
    console.log('SocketIO disconnected');
    addLog('Disconnected from server');
});

// =========================================================================
// API helpers
// =========================================================================

async function apiGet(url) {
    const resp = await fetch(url);
    return resp.json();
}

async function apiPost(url, data) {
    let resp;
    try {
        resp = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: data ? JSON.stringify(data) : '{}',
        });
    } catch (netErr) {
        // Network failure — surface as a toast so the user knows why
        // nothing happened. Then return a synthetic error object so
        // callers can still pattern-match on `.error` / `.ok`.
        showApiError('Network error', netErr.message || String(netErr));
        return { ok: false, error: 'network', message: String(netErr) };
    }

    let body = null;
    try {
        body = await resp.json();
    } catch (_) {
        body = {};
    }

    if (!resp.ok) {
        // Surface HTTP errors (especially the 409 'not_connected' /
        // 'busy' / 'estopped' responses from /api/calibration/*). The
        // server includes a human-readable `message` we can show
        // directly; callers get {ok:false, error, message} so they can
        // avoid locking up the UI as if the command was accepted.
        const errCode = body.error || ('http_' + resp.status);
        const errMsg = body.message || `HTTP ${resp.status}`;
        showApiError(errCode, errMsg);
        return Object.assign({ ok: false }, body, {
            error: errCode, message: errMsg, status: resp.status,
        });
    }

    return Object.assign({ ok: true }, body);
}

// Lightweight toast/alert for API errors. Falls back to alert() if no
// toast container is present. Kept simple on purpose — one place to
// change later if we want a nicer UI.
function showApiError(code, message) {
    console.warn(`[api] ${code}: ${message}`);
    try {
        // Also drop it into the activity log if the helper exists
        if (typeof addLog === 'function') {
            addLog(`Error (${code}): ${message}`);
        }
    } catch (_) {}
    // Simple alert so the user can't miss it — especially the
    // "connect the machine first" case, which is the most common.
    alert(`${message}`);
}

// =========================================================================
// Emergency Stop
// =========================================================================

function emergencyStop() {
    // Fire immediately, don't wait for response
    fetch('/api/estop', { method: 'POST' });
    addLog('!!! EMERGENCY STOP TRIGGERED !!!');
}

// Keyboard shortcut: Escape key for E-stop
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        emergencyStop();
    }
});

// =========================================================================
// State management
// =========================================================================

let currentState = 'disconnected';
let sessionActive = false;

socket.on('sorter_state', (data) => {
    currentState = data.state;
    updateStateBadge();
    updateSessionButtons();
    addLog(`State: ${data.previous || '?'} -> ${data.state}`);
    // Clear motion waypoint queue when session ends or machine stops
    if (data.state === 'idle' || data.state === 'disconnected' || data.state === 'estopped') {
        _motionWaypoints = [];
        _currentWaypointPause = 0;
        _flushPendingBinUpdates();
    }
});

socket.on('hardware_status', (data) => {
    const badge = document.getElementById('connection-badge');
    const hw = document.getElementById('hw-status');
    if (data.connected) {
        badge.className = 'badge bg-success';
        badge.textContent = 'Connected';
        hw.textContent = 'Connected';
    } else {
        badge.className = 'badge bg-secondary';
        badge.textContent = 'Disconnected';
        hw.textContent = 'Disconnected';
    }
});

socket.on('estop_triggered', () => {
    addLog('!!! EMERGENCY STOP — Machine halted !!!');
    alert('EMERGENCY STOP activated. Machine halted.\nUse Reset & Re-home to restart.');
});

function updateStateBadge() {
    const badge = document.getElementById('state-badge');
    const colors = {
        'disconnected': 'bg-secondary',
        'idle': 'bg-info',
        'sorting': 'bg-success',
        'paused': 'bg-warning',
        'estopped': 'bg-danger',
    };
    badge.className = 'badge ' + (colors[currentState] || 'bg-secondary');
    badge.textContent = currentState.toUpperCase();

    // Show Reset & Re-home button only after an e-stop. M999 clears the
    // Marlin halt, then we home — that's the recovery path.
    const resetBtn = document.getElementById('btn-reset-estop');
    if (resetBtn) {
        resetBtn.style.display = (currentState === 'estopped') ? '' : 'none';
    }
}

async function resetAfterEstop() {
    if (!confirm('Reset the machine after emergency stop?\n\n' +
                 'This will send M999 to clear the Marlin halt and ' +
                 're-home all axes. Make sure nothing is blocking the ' +
                 'carriage.')) {
        return;
    }
    try {
        const resp = await fetch('/api/reset-after-estop', {method: 'POST'});
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            alert('Reset failed: ' + (err.error || err.message || resp.status));
            return;
        }
        addLog('Reset-after-estop queued — waiting for re-home');
    } catch (e) {
        alert('Reset failed: ' + e.message);
    }
}

// ---------------------------------------------------------------------------
// Camera health badge — polls /api/camera/status once per second so the user
// always has an at-a-glance indicator of whether the camera is alive, stalled,
// or dead. This is critical because everything the machine does depends on
// the camera; a silent freeze would be devastating without visibility.
// ---------------------------------------------------------------------------
let _cameraStatusLast = null;
async function pollCameraStatus() {
    try {
        const resp = await fetch('/api/camera/status');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const s = await resp.json();
        _cameraStatusLast = s;
        const badge = document.getElementById('camera-badge');
        if (!badge) return;
        const colorMap = {
            'ok': 'bg-success',
            'stalled': 'bg-warning text-dark',
            'dead': 'bg-danger',
            'unknown': 'bg-secondary',
        };
        const cls = colorMap[s.health] || 'bg-secondary';
        badge.className = 'badge ' + cls;
        let text = '\u{1F4F7} ' + (s.health || 'unknown');
        if (s.active && s.fps != null) {
            text += ' ' + s.fps.toFixed(0) + 'fps';
        }
        if (s.reconnects > 0) {
            text += ' \u21BB' + s.reconnects;
        }
        badge.textContent = text;
        badge.title = 'Camera: ' + (s.health || 'unknown')
            + ' | fps=' + (s.fps != null ? s.fps : '?')
            + ' | frames=' + (s.frame_count != null ? s.frame_count : '?')
            + ' | reconnects=' + (s.reconnects || 0)
            + ' | read_failures=' + (s.read_failures || 0)
            + ' | since_frame=' + (s.seconds_since_frame != null ? s.seconds_since_frame + 's' : '?')
            + (s.last_error ? '\nlast_error: ' + s.last_error : '')
            + '\nClick for details';
    } catch (err) {
        const badge = document.getElementById('camera-badge');
        if (badge) {
            badge.className = 'badge bg-danger';
            badge.textContent = '\u{1F4F7} ERR';
            badge.title = 'Camera status fetch failed: ' + err;
        }
    }
}
function showCameraDetails() {
    const s = _cameraStatusLast;
    if (!s) {
        alert('No camera status available yet.');
        return;
    }
    const lines = [
        'Camera Status',
        '─────────────────',
        'active:            ' + s.active,
        'device_index:      ' + s.device_index,
        'health:            ' + s.health,
        'fps:               ' + s.fps,
        'frame_count:       ' + s.frame_count,
        'reconnects:        ' + s.reconnects,
        'read_failures:     ' + s.read_failures,
        'seconds_since_frame: ' + s.seconds_since_frame,
        'last_error:        ' + (s.last_error || '(none)'),
    ];
    alert(lines.join('\n'));
}
// Start polling immediately and every second thereafter.
setInterval(pollCameraStatus, 1000);
pollCameraStatus();

function updateSessionButtons() {
    const sorting = currentState === 'sorting';
    const paused = currentState === 'paused';
    const active = sorting || paused;

    document.getElementById('btn-start-session').disabled = active;
    document.getElementById('btn-detect').disabled = !sorting;
    document.getElementById('btn-continuous').disabled = !sorting;
    document.getElementById('btn-undo').disabled = !sorting || !_undoAvailable;
    document.getElementById('btn-pause').disabled = !sorting;
    document.getElementById('btn-pause').style.display = sorting ? '' : 'none';
    document.getElementById('btn-resume').style.display = paused ? '' : 'none';
    document.getElementById('btn-stop-session').disabled = !active;

    // Disable single detect during continuous
    if (_continuousActive) {
        document.getElementById('btn-detect').disabled = true;
    }

    sessionActive = active;

    // Lock / unlock the sort configuration panel while a session is
    // active. Changing mode mid-sort would desync the worker's active
    // SortConfig from what the UI shows.
    _lockSortConfig(active);
}

function _lockSortConfig(locked) {
    // Sort mode dropdown
    const modeSelect = document.getElementById('sort-mode');
    if (modeSelect) modeSelect.disabled = locked;

    // Bin count, fallback, limit
    for (const id of ['custom-bin-count', 'custom-fallback', 'custom-bin-limit']) {
        const el = document.getElementById(id);
        if (el) el.disabled = locked;
    }

    // All custom query inputs + overflow checkboxes
    document.querySelectorAll('[id^="custom-query-"]').forEach(el => {
        // Don't re-enable inputs that are disabled because they're
        // fallback bins or overflow bins — only toggle the locked state.
        if (!locked) {
            const binNum = parseInt(el.id.replace('custom-query-', ''));
            const fallback = parseInt(document.getElementById('custom-fallback')?.value) || 0;
            const overflowCb = document.getElementById(`custom-overflow-${binNum}`);
            const isOverflow = overflowCb && overflowCb.checked;
            el.disabled = (binNum === fallback) || isOverflow;
        } else {
            el.disabled = true;
        }
    });
    document.querySelectorAll('.overflow-check').forEach(el => {
        el.disabled = locked;
    });
    document.querySelectorAll('[id^="overflow-parent-"]').forEach(el => {
        el.disabled = locked;
    });

    // Config file selector + load/save buttons
    const cfgSelect = document.getElementById('config-file-select');
    if (cfgSelect) cfgSelect.disabled = locked;
    const customLoadFile = document.getElementById('custom-load-file');
    if (customLoadFile) customLoadFile.disabled = locked;

    // Apply button
    const applyBtn = document.getElementById('btn-apply-sort-config');
    if (applyBtn) applyBtn.disabled = locked;

    // Show a lock indicator on the config panel header
    const collapseHeader = document.querySelector('[data-bs-target="#sort-config-collapse"]');
    if (collapseHeader) {
        const hint = collapseHeader.querySelector('.text-muted');
        if (hint) {
            hint.textContent = locked
                ? '🔒 locked while session is active'
                : 'click to show/hide';
        }
    }
}

// =========================================================================
// Card detection events
// =========================================================================

socket.on('card_detected', (data) => {
    const panel = document.getElementById('last-card-info');
    if (data.recognized) {
        const borderBadge = data.border === 'borderless'
            ? ' <span class="badge bg-info">Borderless</span>' : '';
        const layoutBadge = data.layout && data.layout !== 'normal'
            ? ` <span class="badge bg-warning text-dark">${data.layout}</span>` : '';
        panel.innerHTML = `
            <h5>${data.name}${borderBadge}${layoutBadge}</h5>
            <p class="mb-1">Set: <strong>${data.set}</strong> | Colors: <strong>${(data.colors || []).join('')}</strong></p>
            <p class="mb-1">Type: ${(data.types || []).join(' ')} | CMC: ${data.cmc}</p>
            <p class="mb-1">Price: ${data.price} | Rarity: ${data.rarity}</p>
            <p class="mb-0">Bin: <strong>${data.bin}</strong> | Method: <em>${data.method}</em></p>
        `;
        addLog(`Detected: ${data.name} -> Bin ${data.bin} (${data.method})`);
    } else {
        panel.innerHTML = `<p class="text-danger">Unrecognized card -> Bin ${data.bin || 10}</p>`;
        addLog(`Unrecognized card -> Bin ${data.bin || 10}`);
    }
});

// =========================================================================
// Camera Feed Overlay
// =========================================================================

let _overlayTimeout = null;

function drawCameraOverlay(data) {
    const canvas = document.getElementById('camera-overlay-canvas');
    const img = document.getElementById('session-camera-feed');
    if (!canvas || !img) return;

    // Match canvas size to displayed image
    const rect = img.getBoundingClientRect();
    canvas.width = rect.width;
    canvas.height = rect.height;

    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    if (!data) return;

    // Semi-transparent banner at top
    const bannerH = 60;
    ctx.fillStyle = data.recognized ? 'rgba(0, 40, 0, 0.75)' : 'rgba(80, 0, 0, 0.75)';
    ctx.fillRect(0, 0, canvas.width, bannerH);

    // Card name
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 18px sans-serif';
    ctx.textBaseline = 'top';
    const name = data.recognized ? data.name : 'UNRECOGNIZED';
    ctx.fillText(name, 10, 8);

    // Details line
    ctx.font = '13px sans-serif';
    ctx.fillStyle = '#ccc';
    if (data.recognized) {
        const borderTag = data.border === 'borderless' ? ' | BL' : '';
        const details = `${data.set || ''} | ${(data.colors || []).join('')} | Bin ${data.bin} | ${data.method || ''}${borderTag}`;
        ctx.fillText(details, 10, 32);
    } else {
        ctx.fillText(`-> Bin ${data.bin || '?'}`, 10, 32);
    }

    // Price badge (bottom-right)
    if (data.price && data.price !== 'N/A') {
        const priceStr = data.price;
        ctx.font = 'bold 16px sans-serif';
        const pw = ctx.measureText(priceStr).width + 16;
        ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
        ctx.fillRect(canvas.width - pw - 8, canvas.height - 36, pw, 28);
        ctx.fillStyle = '#4f4';
        ctx.fillText(priceStr, canvas.width - pw - 1, canvas.height - 32);
    }

    // Auto-fade after 5 seconds
    clearTimeout(_overlayTimeout);
    _overlayTimeout = setTimeout(() => {
        const c = document.getElementById('camera-overlay-canvas');
        if (c) c.getContext('2d').clearRect(0, 0, c.width, c.height);
    }, 5000);
}

// Hook into card_detected to draw overlay
socket.on('card_detected', (data) => {
    drawCameraOverlay(data);
});

socket.on('card_picked_up', (data) => {
    const el = document.getElementById('suction-status');
    el.textContent = data.name || 'UNKNOWN';
    el.className = 'text-center text-primary';
});

socket.on('card_dropped', (data) => {
    const el = document.getElementById('suction-status');
    el.textContent = 'EMPTY';
    el.className = 'text-center text-muted';
});

socket.on('session_stats', (data) => {
    const panel = document.getElementById('session-stats-panel');
    panel.innerHTML = `
        <p class="mb-1">Scans: <strong>${data.total_scans}</strong> | Recognized: ${data.recognized} | Unrecognized: ${data.unrecognized}</p>
        <p class="mb-1">Recognition rate: <strong>${data.recognition_rate}</strong></p>
        <p class="mb-1">Total value: <strong>${data.total_value}</strong></p>
        <p class="mb-0">Avg speed: ${data.avg_seconds_per_card || '?'}s/card</p>
    `;

    // Dashboard quick stats
    const dashStats = document.getElementById('dashboard-stats');
    dashStats.innerHTML = `
        <p>Active session: <strong>${data.total_scans}</strong> cards sorted |
        Recognition: <strong>${data.recognition_rate}</strong> |
        Value: <strong>${data.total_value}</strong></p>
    `;

    // Stack estimate
    if (data.estimated_cards_remaining !== undefined) {
        const stackPanel = document.getElementById('stack-estimate-panel');
        const mins = Math.round((data.estimated_time_remaining || 0) / 60);
        stackPanel.innerHTML = `
            <p class="mb-1">Est. cards remaining: <strong>${data.estimated_cards_remaining}</strong></p>
            <p class="mb-0">Est. time remaining: <strong>${mins} min</strong></p>
        `;
    }

    // Defer bin content updates until the drop waypoint plays in the animation,
    // so bin counts don't jump ahead of the visual motion.
    if (_motionWaypoints.length > 0) {
        _pendingBinUpdates.push({
            cards_per_bin: data.cards_per_bin,
            bin_contents_detail: data.bin_contents_detail,
            bin_fullness: data.bin_fullness,
        });
    } else {
        updateBinContentsPanel(data.cards_per_bin, data.bin_contents_detail, data.bin_fullness);
    }
});

socket.on('session_started', (data) => {
    addLog(`Session started: mode=${data.mode}, bins=${data.bin_count}`);
    // Start camera feed on session tab
    startCameraFeed('session-camera-feed');
});

// --- Staging background capture prompt (legacy, simple confirm) ---
socket.on('staging_capture_prompt', (data) => {
    // Fallback: simple confirm dialog (used if staging_roi_prompt is not emitted)
    const existing = document.getElementById('staging-capture-overlay');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'staging-capture-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;' +
        'background:rgba(0,0,0,0.7);display:flex;align-items:center;' +
        'justify-content:center;z-index:9999';
    overlay.innerHTML = `
        <div style="background:var(--bg-card);border:1px solid var(--border-card);border-radius:12px;padding:32px 40px;max-width:480px;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,0.5)">
            <h4 style="margin-bottom:16px;color:var(--text-primary)">📷 Staging Background Capture</h4>
            <p style="margin-bottom:8px;font-size:1.05em;color:var(--text-primary)">${data.message || 'Please clear the staging platform.'}</p>
            <button id="btn-confirm-staging-capture" class="btn btn-success btn-lg"
                    style="min-width:200px">
                ✅ Platform is Clear — Continue
            </button>
        </div>`;
    document.body.appendChild(overlay);
    document.getElementById('btn-confirm-staging-capture').addEventListener('click', () => {
        overlay.remove();
        addLog('Staging platform confirmed clear — capturing background...');
        apiPost('/api/session/confirm-staging-capture');
    });
});

// --- Staging ROI drawing prompt ---
socket.on('staging_roi_prompt', (data) => {
    const existing = document.getElementById('staging-capture-overlay');
    if (existing) existing.remove();

    const imgW = data.width || 1080;
    const imgH = data.height || 1920;

    const overlay = document.createElement('div');
    overlay.id = 'staging-capture-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;' +
        'background:rgba(0,0,0,0.85);display:flex;flex-direction:column;' +
        'align-items:center;justify-content:center;z-index:9999;padding:20px;';
    overlay.innerHTML = `
        <div style="background:var(--bg-card);border:1px solid var(--border-card);
                    border-radius:12px;padding:20px 24px;max-width:900px;width:100%;
                    box-shadow:0 8px 32px rgba(0,0,0,0.5);text-align:center;">
            <h4 style="margin-bottom:8px;color:var(--text-primary)">📐 Set Staging Platform Bounds</h4>
            <p style="color:var(--text-secondary);font-size:0.95em;margin-bottom:4px">
                ${data.message || 'Click the 4 corners of the staging platform.'}
            </p>
            <p id="staging-roi-status" style="color:var(--accent-yellow);font-size:0.9em;margin-bottom:12px">
                Click corner 1 of 4 (Top-Left)
            </p>
            <div style="position:relative;display:inline-block;max-height:65vh;cursor:crosshair;">
                <img id="staging-roi-img" src="/api/camera/feed"
                     style="max-width:100%;max-height:65vh;display:block;border-radius:6px;">
                <canvas id="staging-roi-canvas"
                        style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:auto;"></canvas>
            </div>
            <div style="margin-top:12px;display:flex;gap:10px;justify-content:center;">
                <button id="btn-roi-reset" class="btn btn-outline-secondary">Reset</button>
                <button id="btn-roi-confirm" class="btn btn-success" disabled>
                    ✅ Confirm Bounds
                </button>
                <button id="btn-roi-skip" class="btn btn-outline-warning btn-sm">Skip</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);

    const corners = [];
    const cornerLabels = ['Top-Left', 'Top-Right', 'Bottom-Right', 'Bottom-Left'];
    const img = document.getElementById('staging-roi-img');
    const canvas = document.getElementById('staging-roi-canvas');
    const status = document.getElementById('staging-roi-status');
    const ctx = canvas.getContext('2d');

    function syncCanvasSize() {
        canvas.width = img.clientWidth;
        canvas.height = img.clientHeight;
    }

    function drawCorners() {
        syncCanvasSize();
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        if (corners.length === 0) return;

        const scaleX = canvas.width / imgW;
        const scaleY = canvas.height / imgH;

        // Draw lines between corners
        ctx.strokeStyle = '#4caf7c';
        ctx.lineWidth = 2;
        ctx.beginPath();
        for (let i = 0; i < corners.length; i++) {
            const x = corners[i][0] * scaleX;
            const y = corners[i][1] * scaleY;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        if (corners.length === 4) {
            ctx.lineTo(corners[0][0] * scaleX, corners[0][1] * scaleY);
        }
        ctx.stroke();

        // Draw corner dots with labels
        for (let i = 0; i < corners.length; i++) {
            const x = corners[i][0] * scaleX;
            const y = corners[i][1] * scaleY;
            ctx.fillStyle = '#4caf7c';
            ctx.beginPath();
            ctx.arc(x, y, 6, 0, Math.PI * 2);
            ctx.fill();
            ctx.fillStyle = '#fff';
            ctx.font = '12px sans-serif';
            ctx.fillText(cornerLabels[i], x + 10, y - 8);
        }

        // Semi-transparent fill inside polygon
        if (corners.length === 4) {
            ctx.fillStyle = 'rgba(76, 175, 124, 0.15)';
            ctx.beginPath();
            ctx.moveTo(corners[0][0] * scaleX, corners[0][1] * scaleY);
            for (let i = 1; i < 4; i++) {
                ctx.lineTo(corners[i][0] * scaleX, corners[i][1] * scaleY);
            }
            ctx.closePath();
            ctx.fill();
        }
    }

    // MJPEG fires onload on every frame — only use it for canvas resizing
    img.onload = () => {
        syncCanvasSize();
        drawCorners();
    };

    // Click handler — set up once (not inside onload)
    canvas.addEventListener('click', (e) => {
        if (corners.length >= 4) return;
        const rect = canvas.getBoundingClientRect();
        const clickX = e.clientX - rect.left;
        const clickY = e.clientY - rect.top;
        // Convert to image pixel coordinates
        const px = Math.round(clickX / canvas.width * imgW);
        const py = Math.round(clickY / canvas.height * imgH);
        corners.push([px, py]);
        drawCorners();

        if (corners.length < 4) {
            status.textContent = `Click corner ${corners.length + 1} of 4 (${cornerLabels[corners.length]})`;
        } else {
            status.textContent = 'All 4 corners set — click Confirm or Reset';
            status.style.color = 'var(--accent-green)';
            document.getElementById('btn-roi-confirm').disabled = false;
        }
    });

    document.getElementById('btn-roi-reset').addEventListener('click', () => {
        corners.length = 0;
        drawCorners();
        status.textContent = 'Click corner 1 of 4 (Top-Left)';
        status.style.color = 'var(--accent-yellow)';
        document.getElementById('btn-roi-confirm').disabled = true;
    });

    // Stop MJPEG stream when overlay closes (free bandwidth)
    function stopStream() {
        img.src = '';
    }

    document.getElementById('btn-roi-confirm').addEventListener('click', () => {
        stopStream();
        overlay.remove();
        addLog('Staging ROI set — saving...');
        apiPost('/api/session/set-staging-roi', { corners: corners });
    });

    document.getElementById('btn-roi-skip').addEventListener('click', () => {
        stopStream();
        overlay.remove();
        addLog('Staging ROI skipped — using full-frame detection');
        apiPost('/api/session/confirm-staging-capture');
    });
});

socket.on('staging_capture_result', (data) => {
    // Remove overlay if it's still showing (e.g. timeout case)
    const overlay = document.getElementById('staging-capture-overlay');
    if (overlay) overlay.remove();

    if (data.ok) {
        addLog(`✅ ${data.message}`);
    } else {
        addLog(`⚠️ Staging capture: ${data.message}`);
    }
});

// --- Focus confirmation prompt ---
// Shown on the first card of a session so the user can verify the
// live camera feed is sharp before we lock autofocus for the rest
// of the session.
socket.on('focus_confirm_prompt', (data) => {
    const existing = document.getElementById('focus-confirm-overlay');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'focus-confirm-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;' +
        'background:rgba(0,0,0,0.85);display:flex;flex-direction:column;' +
        'align-items:center;justify-content:center;z-index:9999;padding:20px;';
    overlay.innerHTML = `
        <div style="background:var(--bg-card);border:1px solid var(--border-card);
                    border-radius:12px;padding:20px 24px;max-width:800px;width:100%;
                    box-shadow:0 8px 32px rgba(0,0,0,0.5);text-align:center;">
            <h4 style="margin-bottom:8px;color:var(--text-primary)">🔍 Confirm Focus</h4>
            <p style="color:var(--text-secondary);font-size:0.95em;margin-bottom:12px">
                ${data.message || 'Check that the card is in focus, then lock.'}
            </p>
            <div style="position:relative;display:inline-block;max-height:60vh;">
                <img id="focus-confirm-feed" src="/api/camera/feed?t=${Date.now()}"
                     style="max-width:100%;max-height:60vh;display:block;border-radius:6px;">
            </div>
            <div style="margin-top:12px;display:flex;gap:10px;justify-content:center;">
                <button id="btn-focus-lock" class="btn btn-success btn-lg">
                    🔒 Lock Focus
                </button>
            </div>
        </div>`;
    document.body.appendChild(overlay);

    document.getElementById('btn-focus-lock').addEventListener('click', () => {
        overlay.remove();
        addLog('Focus confirmed — locking for session');
        apiPost('/api/session/confirm-focus');
    });
});

socket.on('session_ended', (data) => {
    addLog(`Session ended: ${data.total_scans} scans`);
    stopCameraFeed('session-camera-feed');
    _continuousActive = false;
    _undoAvailable = false;
    const btn = document.getElementById('btn-continuous');
    btn.classList.remove('btn-primary');
    btn.classList.add('btn-outline-primary');
    btn.innerHTML = '&#9654; Continuous';
    document.getElementById('btn-undo').disabled = true;
});

socket.on('bin_update', (data) => {
    loadBinTable();
});

socket.on('probe_result', (data) => {
    addLog(`Probe bin ${data.bin}: X=${data.x}mm, Z=${data.z}mm`);
});

// Test scan events
socket.on('test_scan_progress', (data) => {
    const prog = document.getElementById('test-scan-progress');
    if (prog) prog.textContent = `Scanning card ${data.current} of ${data.total}...`;
});

socket.on('test_scan_card', (data) => {
    const el = document.getElementById('test-scan-results');
    if (!el) return;
    const blTag = data.border === 'borderless' ? ' <span class="badge bg-info">BL</span>' : '';
    const badge = data.recognized
        ? `<span class="text-success">${data.name}</span>${blTag} <span class="text-muted">(${data.method}${data.set ? ', ' + data.set : ''})</span>`
        : `<span class="text-danger">Unrecognized</span>`;
    el.innerHTML += `<div>#${data.index}: ${badge}</div>`;
    el.scrollTop = el.scrollHeight;
});

socket.on('test_scan_finished', (data) => {
    const prog = document.getElementById('test-scan-progress');
    if (data.error) {
        if (prog) prog.textContent = `Error: ${data.error}`;
    } else {
        if (prog) prog.textContent = `Done: ${data.recognized}/${data.total} recognized`;
    }
    _testScanDone();
});

// =========================================================================
// Motion tracking
// =========================================================================

// Motion state: display position animates smoothly through waypoint queue
let motionState = { x: 0, z: 220, dest_x: 0, dest_z: 220, moving: false, carrying: null };
let _motionDisplay = { x: 0, z: 220 };  // Smoothly interpolated display position
let _motionAnimating = false;
let _lastAnimFrame = 0;

// Waypoint queue for realistic motion preview
// Each waypoint: {x, z, feedrate, carrying, pause?}
// feedrate is mm/min (0 = instant/pause), pause is ms
let _motionWaypoints = [];
let _currentWaypointPause = 0;  // remaining pause time in seconds

// Deferred bin updates — queued from session_stats, applied when a drop
// waypoint plays (carrying transitions to null). This prevents bin counts
// from updating before the animation shows the card being dropped.
let _pendingBinUpdates = [];

// Speed multiplier: 1 = real time, 10 = 10x faster
let _motionSpeedMultiplier = 1;

function setMotionSpeed(multiplier) {
    _motionSpeedMultiplier = multiplier;
    // Update button active states
    document.querySelectorAll('[id^="btn-speed-"]').forEach(btn => btn.classList.remove('active'));
    const activeBtn = document.getElementById('btn-speed-' + multiplier + 'x');
    if (activeBtn) activeBtn.classList.add('active');
}

function _flushPendingBinUpdates() {
    // Apply all deferred bin updates at once (uses the latest one)
    if (_pendingBinUpdates.length > 0) {
        const latest = _pendingBinUpdates[_pendingBinUpdates.length - 1];
        _pendingBinUpdates = [];
        _applyBinUpdate(latest);
    }
}

function _applyBinUpdate(update) {
    if (update.bin_contents_detail !== undefined) {
        // Full update from session_stats
        updateBinContentsPanel(update.cards_per_bin, update.bin_contents_detail, update.bin_fullness);
    } else {
        // Counts-only update from sim_progress / sim poll
        _cachedBinCounts = update.cards_per_bin;
        drawMotionCanvas();
        drawBinLayoutCanvas();
    }
}

socket.on('motion_path', (waypoints) => {
    // Append new waypoints to the queue (don't replace — continuous sorting
    // sends a new path per card, and we want to animate them all in sequence)
    for (const wp of waypoints) {
        _motionWaypoints.push(wp);
    }
    // Start animation if not running
    if (!_motionAnimating) {
        _motionAnimating = true;
        _lastAnimFrame = performance.now();
        requestAnimationFrame(_animateMotion);
    }
});

socket.on('motion_update', (data) => {
    motionState = data;
    // Only use motion_update as fallback when no waypoints are queued
    if (_motionWaypoints.length === 0) {
        // Snap to reported position when not animating a path
        _motionDisplay.x = data.x;
        _motionDisplay.z = data.z;
        if (data.carrying !== undefined) {
            motionState.carrying = data.carrying;
        }
        drawMotionCanvas();
    }
});

function _animateMotion(timestamp) {
    const rawDt = Math.min((timestamp - _lastAnimFrame) / 1000.0, 0.1);  // seconds, cap at 100ms
    const dt = rawDt * _motionSpeedMultiplier;  // apply speed multiplier
    _lastAnimFrame = timestamp;

    if (_motionWaypoints.length === 0) {
        // No waypoints — stop animating, flush any pending bin updates
        motionState.moving = false;
        _motionAnimating = false;
        _flushPendingBinUpdates();
        drawMotionCanvas();
        return;
    }

    // Handle pause on current waypoint
    if (_currentWaypointPause > 0) {
        _currentWaypointPause -= dt;
        if (_currentWaypointPause > 0) {
            drawMotionCanvas();
            requestAnimationFrame(_animateMotion);
            return;
        }
        // Pause finished — advance to next waypoint
        _currentWaypointPause = 0;
        _motionWaypoints.shift();
        if (_motionWaypoints.length === 0) {
            motionState.moving = false;
            _motionAnimating = false;
            _flushPendingBinUpdates();
            drawMotionCanvas();
            return;
        }
    }

    const wp = _motionWaypoints[0];

    // Update carrying state from waypoint — detect drops to apply deferred bin updates
    if (wp.carrying !== undefined) {
        const wasCarrying = motionState.carrying;
        motionState.carrying = wp.carrying;
        // Card was just dropped (carrying went from a name to null)
        if (wasCarrying && !wp.carrying && _pendingBinUpdates.length > 0) {
            const update = _pendingBinUpdates.shift();
            _applyBinUpdate(update);
        }
    }

    // Update destination in motionState for the destination marker
    motionState.dest_x = wp.x;
    motionState.dest_z = wp.z;
    motionState.moving = true;

    // Calculate speed from feedrate (mm/min -> mm/s)
    // feedrate=0 means instant position change (vacuum/pressure pause)
    const speed = wp.feedrate > 0 ? wp.feedrate / 60.0 : 99999;

    // The machine moves one axis at a time. Determine which axis to move.
    // Convention: Z moves complete before X moves (if both differ).
    // Each waypoint should only change one axis, but handle both just in case.
    const dz = wp.z - _motionDisplay.z;
    const dx = wp.x - _motionDisplay.x;

    let arrived = false;

    if (Math.abs(dz) > 0.3) {
        // Move Z first
        const maxStep = speed * dt;
        if (Math.abs(dz) <= maxStep) {
            _motionDisplay.z = wp.z;
        } else {
            _motionDisplay.z += Math.sign(dz) * maxStep;
        }
    } else if (Math.abs(dx) > 0.3) {
        // Then move X
        _motionDisplay.z = wp.z;  // snap Z exactly
        const maxStep = speed * dt;
        if (Math.abs(dx) <= maxStep) {
            _motionDisplay.x = wp.x;
        } else {
            _motionDisplay.x += Math.sign(dx) * maxStep;
        }
    } else {
        // Arrived at this waypoint
        _motionDisplay.x = wp.x;
        _motionDisplay.z = wp.z;
        arrived = true;
    }

    // Sync motionState position for drawing
    motionState.x = _motionDisplay.x;
    motionState.z = _motionDisplay.z;

    drawMotionCanvas();

    if (arrived) {
        // Check for pause on this waypoint
        if (wp.pause && wp.pause > 0) {
            _currentWaypointPause = wp.pause / 1000.0;  // ms -> seconds
            requestAnimationFrame(_animateMotion);
            return;
        }
        // No pause — advance immediately
        _motionWaypoints.shift();
        if (_motionWaypoints.length === 0) {
            motionState.moving = false;
            _motionAnimating = false;
            _flushPendingBinUpdates();
            drawMotionCanvas();
            return;
        }
    }

    requestAnimationFrame(_animateMotion);
}

// Periodic sync for when we miss events (fallback, not primary driver)
setInterval(async () => {
    if (currentState === 'sorting' && _motionWaypoints.length === 0) {
        try {
            const data = await apiGet('/api/motion/position');
            motionState = data;
            _motionDisplay.x = data.x;
            _motionDisplay.z = data.z;
            drawMotionCanvas();
        } catch (e) {}
    }
}, 2000);

// =========================================================================
// Motion Canvas
// =========================================================================

function drawMotionCanvas() {
    const canvas = document.getElementById('motion-canvas');
    if (!canvas) return;
    // Responsive: match canvas pixels to displayed size
    const displayW = canvas.clientWidth || 800;
    const displayH = Math.max(200, Math.min(300, displayW * 0.375));
    canvas.width = displayW;
    canvas.height = displayH;
    const ctx = canvas.getContext('2d');
    const W = canvas.width;
    const H = canvas.height;

    // Machine dimensions
    const X_MAX = 1100;
    const Z_MAX = 220;
    const margin = 40;

    const scaleX = (x) => margin + (x / X_MAX) * (W - 2 * margin);
    const scaleZ = (z) => margin + ((Z_MAX - z) / Z_MAX) * (H - 2 * margin); // Z inverted: top=Z_MAX

    ctx.clearRect(0, 0, W, H);

    // Background
    ctx.fillStyle = '#1a1a2e';
    ctx.fillRect(0, 0, W, H);

    // Grid lines
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 0.5;
    for (let x = 0; x <= X_MAX; x += 100) {
        const sx = scaleX(x);
        ctx.beginPath(); ctx.moveTo(sx, margin); ctx.lineTo(sx, H - margin); ctx.stroke();
        ctx.fillStyle = '#666';
        ctx.font = '10px monospace';
        ctx.fillText(x + 'mm', sx - 12, H - 5);
    }
    for (let z = 0; z <= Z_MAX; z += 50) {
        const sy = scaleZ(z);
        ctx.beginPath(); ctx.moveTo(margin, sy); ctx.lineTo(W - margin, sy); ctx.stroke();
        ctx.fillStyle = '#666';
        ctx.fillText(z + '', 5, sy + 4);
    }

    // Draw detection/staging platform (~200mm wide, between source and first sort bin)
    // This is the white surface the camera looks at. Sits at same height as bin tops.
    const STAGING_X = _machinePositions.staging_x;
    const PLATFORM_WIDTH = _machinePositions.staging_width;
    const binHeight = 100;           // height in Z units (same as bins)
    const platLeft = scaleX(STAGING_X - PLATFORM_WIDTH / 2);
    const platRight = scaleX(STAGING_X + PLATFORM_WIDTH / 2);
    const platW = platRight - platLeft;
    const platH = (binHeight / Z_MAX) * (H - 2 * margin);
    const platTop = scaleZ(0) - platH;
    // Platform body
    ctx.fillStyle = '#3a3a4e';
    ctx.strokeStyle = '#777';
    ctx.lineWidth = 1.5;
    ctx.fillRect(platLeft, platTop, platW, platH);
    ctx.strokeRect(platLeft, platTop, platW, platH);
    // White staging surface on top of platform
    ctx.fillStyle = '#ccc';
    ctx.fillRect(platLeft + 2, platTop, platW - 4, 3);
    // Platform label
    ctx.fillStyle = '#aaa';
    ctx.font = '10px monospace';
    ctx.textAlign = 'center';
    ctx.fillText('Staging', scaleX(STAGING_X), platTop + platH / 2 + 4);
    ctx.textAlign = 'left';

    // Bin 0 (source bin) — drawn as its own bin, next to the platform
    const SOURCE_X = _machinePositions.source_x;
    const srcBinW = (80 / X_MAX) * (W - 2 * margin);
    const srcBinH = platH;
    const srcBinTop = platTop;
    ctx.fillStyle = '#2a3a4e';
    ctx.strokeStyle = '#6af';
    ctx.lineWidth = 1.5;
    ctx.fillRect(scaleX(SOURCE_X) - srcBinW/2, srcBinTop, srcBinW, srcBinH);
    ctx.strokeRect(scaleX(SOURCE_X) - srcBinW/2, srcBinTop, srcBinW, srcBinH);
    ctx.fillStyle = '#6af';
    ctx.font = 'bold 10px monospace';
    ctx.textAlign = 'center';
    ctx.fillText('Source', scaleX(SOURCE_X), srcBinTop - 5);
    ctx.font = '9px monospace';
    ctx.fillText('Bin 0', scaleX(SOURCE_X), srcBinTop + srcBinH / 2 + 4);
    ctx.textAlign = 'left';

    // Camera icon above detection position
    const DETECT_X = _machinePositions.detection_x;
    const detectSx = scaleX(DETECT_X);
    const camY = margin - 18;
    // Camera body
    ctx.fillStyle = '#f80';
    ctx.fillRect(detectSx - 10, camY, 20, 12);
    // Viewfinder bump
    ctx.beginPath();
    ctx.moveTo(detectSx + 10, camY + 2);
    ctx.lineTo(detectSx + 16, camY - 1);
    ctx.lineTo(detectSx + 16, camY + 13);
    ctx.lineTo(detectSx + 10, camY + 10);
    ctx.fill();
    // Lens
    ctx.beginPath();
    ctx.arc(detectSx, camY + 6, 4, 0, Math.PI * 2);
    ctx.fillStyle = '#1a1a2e';
    ctx.fill();
    ctx.strokeStyle = '#ffa500';
    ctx.lineWidth = 1;
    ctx.stroke();
    // Dashed FOV lines from camera down to platform
    ctx.strokeStyle = 'rgba(255, 136, 0, 0.3)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(detectSx - 8, camY + 12);
    ctx.lineTo(detectSx - 20, platTop);
    ctx.moveTo(detectSx + 8, camY + 12);
    ctx.lineTo(detectSx + 20, platTop);
    ctx.stroke();
    ctx.setLineDash([]);

    // Draw sort bins as rectangles (skip bin 0 — drawn above as source bin)
    const binWidth = 80;
    const bins = _cachedBinLocations || {};
    for (const [binNum, xPos] of Object.entries(bins)) {
        if (parseInt(binNum) === 0) continue;
        const bx = scaleX(xPos) - binWidth / (2 * X_MAX) * (W - 2 * margin);
        const by = scaleZ(0);
        ctx.fillStyle = '#2a2a3e';
        ctx.strokeStyle = '#555';
        ctx.lineWidth = 1;
        const bw = (binWidth / X_MAX) * (W - 2 * margin);
        const bh = (binHeight / Z_MAX) * (H - 2 * margin);
        ctx.fillRect(scaleX(xPos) - bw/2, by - bh, bw, bh);
        ctx.strokeRect(scaleX(xPos) - bw/2, by - bh, bw, bh);
        // Label
        ctx.fillStyle = '#aaa';
        ctx.font = '11px monospace';
        ctx.textAlign = 'center';
        ctx.fillText('Bin ' + binNum, scaleX(xPos), by - bh - 5);
        // Card count
        const count = _cachedBinCounts[binNum] || 0;
        if (count > 0) {
            ctx.fillStyle = '#8f8';
            ctx.fillText(count + (count === 1 ? ' card' : ' cards'), scaleX(xPos), by - bh / 2);
        }
        ctx.textAlign = 'left';
    }

    // Destination marker (ghost) — shows next waypoint target
    const isMoving = _motionWaypoints.length > 0 || motionState.moving;

    if (isMoving && _motionWaypoints.length > 0) {
        // Show current waypoint target
        const wp = _motionWaypoints[0];
        ctx.beginPath();
        ctx.arc(scaleX(wp.x), scaleZ(wp.z), 8, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(255, 255, 0, 0.6)';
        ctx.lineWidth = 2;
        ctx.setLineDash([4, 4]);
        ctx.stroke();
        ctx.setLineDash([]);
    } else if (isMoving) {
        // Fallback: use motionState destination
        ctx.beginPath();
        ctx.arc(scaleX(motionState.dest_x), scaleZ(motionState.dest_z), 8, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(255, 255, 0, 0.6)';
        ctx.lineWidth = 2;
        ctx.setLineDash([4, 4]);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    // Current position (suction head) — uses smoothly animated display position
    const headX = scaleX(_motionDisplay.x);
    const headZ = scaleZ(_motionDisplay.z);

    // Draw arm from top (vertical shaft)
    ctx.strokeStyle = '#888';
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(headX, margin);
    ctx.lineTo(headX, headZ);
    ctx.stroke();

    // Head circle
    ctx.beginPath();
    ctx.arc(headX, headZ, 8, 0, Math.PI * 2);
    ctx.fillStyle = motionState.carrying ? '#0af' : '#0f0';
    ctx.fill();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.stroke();

    // Glow effect when carrying
    if (motionState.carrying) {
        ctx.beginPath();
        ctx.arc(headX, headZ, 12, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(0, 170, 255, 0.3)';
        ctx.lineWidth = 3;
        ctx.stroke();
    }

    // Label for carrying
    if (motionState.carrying) {
        ctx.fillStyle = '#0af';
        ctx.font = '10px sans-serif';
        ctx.fillText(motionState.carrying, headX + 12, headZ - 5);
    }

    // Position readout
    const dispX = Math.round(_motionDisplay.x);
    const dispZ = Math.round(_motionDisplay.z);
    ctx.fillStyle = '#ccc';
    ctx.font = '12px monospace';
    ctx.fillText(`X: ${dispX}mm  Z: ${dispZ}mm`, margin, 15);
    if (isMoving && _motionWaypoints.length > 0) {
        const wp = _motionWaypoints[0];
        ctx.fillStyle = '#ff0';
        ctx.fillText(`\u2192 X: ${Math.round(wp.x)}  Z: ${Math.round(wp.z)}`, margin + 220, 15);
    } else if (isMoving) {
        ctx.fillStyle = '#ff0';
        ctx.fillText(`\u2192 X: ${motionState.dest_x}  Z: ${motionState.dest_z}`, margin + 220, 15);
    }
}

// =========================================================================
// Bin Layout Canvas (Bin Setup tab — top-down view of physical layout)
// =========================================================================

function drawBinLayoutCanvas() {
    const canvas = document.getElementById('bin-layout-canvas');
    if (!canvas) return;
    const displayW = canvas.clientWidth || 800;
    const displayH = 160;
    canvas.width = displayW;
    canvas.height = displayH;
    const ctx = canvas.getContext('2d');
    const W = canvas.width;
    const H = canvas.height;

    const X_MAX = 1100;
    const marginL = 40;
    const marginR = 20;
    const marginT = 30;
    const marginB = 25;

    const scaleX = (x) => marginL + (x / X_MAX) * (W - marginL - marginR);

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#1a1a2e';
    ctx.fillRect(0, 0, W, H);

    // Rail line
    ctx.strokeStyle = '#444';
    ctx.lineWidth = 2;
    const railY = marginT + 5;
    ctx.beginPath();
    ctx.moveTo(marginL, railY);
    ctx.lineTo(W - marginR, railY);
    ctx.stroke();
    ctx.fillStyle = '#555';
    ctx.font = '9px monospace';
    ctx.fillText('X rail', marginL, railY - 4);

    // X axis ticks
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 0.5;
    for (let x = 0; x <= X_MAX; x += 100) {
        const sx = scaleX(x);
        ctx.beginPath();
        ctx.moveTo(sx, railY + 4);
        ctx.lineTo(sx, H - marginB);
        ctx.stroke();
        ctx.fillStyle = '#555';
        ctx.font = '9px monospace';
        ctx.textAlign = 'center';
        ctx.fillText(x + '', sx, H - 5);
    }
    ctx.textAlign = 'left';

    // Source bin (Bin 0) — separate from staging platform
    const srcX = scaleX(_machinePositions.source_x);
    const binY = railY + 18;
    const binH = H - marginB - binY;
    const binW = (80 / X_MAX) * (W - marginL - marginR);
    ctx.fillStyle = '#2a3a4e';
    ctx.strokeStyle = '#6af';
    ctx.lineWidth = 1.5;
    ctx.fillRect(srcX - binW/2, binY, binW, binH);
    ctx.strokeRect(srcX - binW/2, binY, binW, binH);
    ctx.fillStyle = '#6af';
    ctx.font = 'bold 9px monospace';
    ctx.textAlign = 'center';
    ctx.fillText('Source', srcX, binY + binH / 2 - 2);
    ctx.fillText('Bin 0', srcX, binY + binH / 2 + 10);

    // Staging platform
    const STAGING_X = _machinePositions.staging_x;
    const PLAT_W = _machinePositions.staging_width;
    const platL = scaleX(STAGING_X - PLAT_W / 2);
    const platR = scaleX(STAGING_X + PLAT_W / 2);
    ctx.fillStyle = '#3a3a4e';
    ctx.strokeStyle = '#777';
    ctx.lineWidth = 1.5;
    ctx.fillRect(platL, binY, platR - platL, binH);
    ctx.strokeRect(platL, binY, platR - platL, binH);
    // White surface
    ctx.fillStyle = '#ccc';
    ctx.fillRect(platL + 2, binY, platR - platL - 4, 3);
    // Labels
    ctx.fillStyle = '#aaa';
    ctx.font = '10px monospace';
    ctx.fillText('Staging', scaleX(STAGING_X), binY + binH / 2 + 4);
    // Camera marker
    const camSx = scaleX(_machinePositions.detection_x);
    ctx.fillStyle = '#f80';
    ctx.font = 'bold 9px monospace';
    ctx.fillText('CAM', camSx, binY + 14);
    ctx.textAlign = 'left';

    // Sort bins
    const bins = _cachedBinLocations || {};
    for (const [binNum, xPos] of Object.entries(bins)) {
        if (parseInt(binNum) === 0) continue;  // skip source bin (drawn above)
        const bx = scaleX(xPos);
        ctx.fillStyle = '#2a2a3e';
        ctx.strokeStyle = '#555';
        ctx.lineWidth = 1;
        ctx.fillRect(bx - binW/2, binY, binW, binH);
        ctx.strokeRect(bx - binW/2, binY, binW, binH);
        // Label
        ctx.fillStyle = '#aaa';
        ctx.font = '10px monospace';
        ctx.textAlign = 'center';
        ctx.fillText('Bin ' + binNum, bx, binY + binH / 2 + 4);
        // Card count
        const count = _cachedBinCounts[binNum] || 0;
        if (count > 0) {
            ctx.fillStyle = '#8f8';
            ctx.font = '9px monospace';
            ctx.fillText(count + '', bx, binY + binH / 2 + 16);
        }
        ctx.textAlign = 'left';
    }
}

let _cachedBinLocations = {};
let _cachedBinCounts = {};

// Cached machine positions for canvas rendering
let _machinePositions = { source_x: 50, detection_x: 100, staging_x: 162, staging_width: 200 };

async function loadBinLocations() {
    try {
        const data = await apiGet('/api/bins/config');
        _cachedBinLocations = data.locations || {};
        // Cache machine positions
        if (data.source_x !== undefined) _machinePositions.source_x = data.source_x;
        if (data.detection_x !== undefined) _machinePositions.detection_x = data.detection_x;
        if (data.staging_x !== undefined) _machinePositions.staging_x = data.staging_x;
        if (data.staging_width !== undefined) _machinePositions.staging_width = data.staging_width;
        // Update machine position inputs (config card)
        const srcInput = document.getElementById('machine-source-x');
        const detInput = document.getElementById('machine-detect-x');
        const stgInput = document.getElementById('machine-staging-x');
        const stgWInput = document.getElementById('machine-staging-w');
        if (srcInput) srcInput.value = _machinePositions.source_x;
        if (detInput) detInput.value = Math.round((_machinePositions.detection_x - _machinePositions.source_x) * 10) / 10;
        if (stgInput) stgInput.value = _machinePositions.staging_x;
        if (stgWInput) stgWInput.value = _machinePositions.staging_width;

        loadBinTable();
        drawMotionCanvas();
        drawBinLayoutCanvas();
    } catch (e) {}
}

// =========================================================================
// Bin table
// =========================================================================

let _cachedBinFullness = {};  // from /api/bins/fullness

async function loadBinTable() {
    try {
        const [configData, fullnessData] = await Promise.all([
            apiGet('/api/bins/config'),
            apiGet('/api/bins/fullness'),
        ]);
        _cachedBinLocations = configData.locations || {};
        _cachedBinFullness = fullnessData;
        const binCounts = fullnessData.bin_card_counts || {};
        const binsFull = new Set(fullnessData.bins_full || []);
        const limit = fullnessData.bin_card_limit || 150;

        const tbody = document.getElementById('bin-table-body');
        tbody.innerHTML = '';

        // Machine position rows (staging platform, camera offset)
        const camOffset = _machinePositions.detection_x - _machinePositions.source_x;

        const stagingRow = document.createElement('tr');
        stagingRow.innerHTML = `
            <td>Staging</td>
            <td>
                <div class="d-flex gap-1 align-items-center">
                    <input type="number" class="form-control form-control-sm" id="tbl-staging-x" value="${_machinePositions.staging_x}" step="0.1" min="0" style="width:80px;">
                    <span class="text-muted small" style="white-space:nowrap;">W:</span>
                    <input type="number" class="form-control form-control-sm" id="tbl-staging-w" value="${_machinePositions.staging_width}" step="1" min="10" style="width:65px;">
                </div>
            </td>
            <td>--</td><td>--</td><td>--</td>
            <td><button class="btn btn-outline-primary btn-sm py-0 px-1" onclick="apiPost('/api/motion/move-x', {x: ${_machinePositions.staging_x}})">Go</button></td>
        `;
        tbody.appendChild(stagingRow);

        const camRow = document.createElement('tr');
        camRow.innerHTML = `
            <td>Camera Offset</td>
            <td><input type="number" class="form-control form-control-sm" id="tbl-cam-offset" value="${camOffset}" step="0.1" min="0" style="width:80px;"></td>
            <td>--</td><td>--</td><td>--</td>
            <td><button class="btn btn-outline-primary btn-sm py-0 px-1" onclick="apiPost('/api/motion/move-x', {x: ${_machinePositions.detection_x}})">Go</button></td>
        `;
        tbody.appendChild(camRow);

        // Bin rows
        for (const [binNum, xPos] of Object.entries(configData.locations).sort((a, b) => parseInt(a[0]) - parseInt(b[0]))) {
            const sessionCount = _cachedBinCounts[binNum] || 0;
            const fullnessCount = binCounts[binNum] || 0;
            const isFull = binsFull.has(parseInt(binNum));
            const isSource = binNum === '0';
            const row = document.createElement('tr');
            if (isFull) row.className = 'table-danger';
            row.innerHTML = `
                <td>${isSource ? '0 (Source)' : binNum}</td>
                <td><input type="number" class="form-control form-control-sm bin-x-input" id="bin-x-${binNum}" value="${xPos}" step="0.1" min="0" style="width:80px;"></td>
                <td id="probe-z-${binNum}">--</td>
                <td>${isSource ? sessionCount : fullnessCount + '/' + limit}</td>
                <td>${isSource ? '--' : (isFull
                    ? '<span class="badge bg-danger">FULL</span>'
                    : '<span class="badge bg-success">OK</span>')}</td>
                <td>
                    <button class="btn btn-outline-primary btn-sm py-0 px-1" onclick="apiPost('/api/bins/test/${binNum}')">Go</button>
                    <button class="btn btn-outline-warning btn-sm py-0 px-1" onclick="apiPost('/api/bins/probe/${binNum}')">Probe</button>
                    ${!isSource ? '<button class="btn btn-outline-success btn-sm py-0 px-1" onclick="markBinEmpty(' + binNum + ')">Empty</button>' : ''}
                </td>
            `;
            tbody.appendChild(row);
        }

        // Sync card limit input
        const limitInput = document.getElementById('bin-card-limit');
        if (limitInput && fullnessData.bin_card_limit) {
            limitInput.value = fullnessData.bin_card_limit;
        }
    } catch (e) {}
}

async function markBinEmpty(binNumber) {
    await apiPost('/api/bins/mark-empty', { bin: binNumber });
    // Table will refresh from the bin_emptied SocketIO event
}

function configureBins() {
    const count = parseInt(document.getElementById('bin-count').value);
    const spacing = parseFloat(document.getElementById('bin-spacing').value);
    const startX = parseFloat(document.getElementById('bin-start-x').value);
    const cardLimit = parseInt(document.getElementById('bin-card-limit').value) || 150;
    apiPost('/api/bins/config', { count, spacing, start_x: startX }).then(() => {
        // Also set the card limit
        apiPost('/api/bins/card-limit', { limit: cardLimit });
        setTimeout(() => { loadBinTable(); drawBinLayoutCanvas(); }, 500);
    });
}

socket.on('bins_configured', (data) => {
    loadBinTable();
    drawBinLayoutCanvas();
    // Sync bin count to the sort config tab
    if (data && data.count) {
        const customBinCount = document.getElementById('custom-bin-count');
        if (customBinCount) customBinCount.value = data.count;
    }
});

// =========================================================================
// Bin config save/load
// =========================================================================

async function loadBinConfigList() {
    try {
        const data = await apiGet('/api/bins/saved-configs');
        const select = document.getElementById('bin-config-select');
        select.innerHTML = '<option value="">-- Select a saved config --</option>';
        for (const cfg of data.configs || []) {
            select.innerHTML += `<option value="${cfg.filename}">${cfg.name} (${cfg.bin_count} bins, ${cfg.spacing}mm)</option>`;
        }
    } catch (e) {}
}

async function saveBinConfig() {
    let name = document.getElementById('bin-config-name').value.trim();
    if (!name) {
        name = prompt('Enter a name for this bin configuration:');
        if (!name) return;
    }
    const filename = name.replace(/[^a-zA-Z0-9_-]/g, '_') + '.json';
    await apiPost(`/api/bins/saved-configs/${filename}`, { name });
    document.getElementById('bin-config-name').value = '';
    addLog(`Bin config saved: ${name}`);
    loadBinConfigList();
}

async function loadBinConfig() {
    const filename = document.getElementById('bin-config-select').value;
    if (!filename) return;
    try {
        const data = await apiGet(`/api/bins/saved-configs/${filename}`);
        // Apply the loaded config
        document.getElementById('bin-count').value = data.bin_count || 10;
        document.getElementById('bin-spacing').value = data.spacing || 100;
        document.getElementById('bin-start-x').value = data.start_x || 100;

        // Restore machine positions if saved
        if (data.source_x || data.detection_x || data.staging_x || data.staging_width) {
            const posPayload = {};
            if (data.source_x) posPayload.source_x = data.source_x;
            if (data.detection_x) posPayload.detection_x = data.detection_x;
            if (data.staging_x) posPayload.staging_x = data.staging_x;
            if (data.staging_width) posPayload.staging_width = data.staging_width;
            await apiPost('/api/bins/machine-positions', posPayload);
            // Update UI fields
            if (data.source_x) document.getElementById('machine-source-x').value = data.source_x;
            if (data.detection_x) document.getElementById('machine-detect-x').value = data.detection_x;
            if (data.staging_x) document.getElementById('machine-staging-x').value = data.staging_x;
        }

        // If saved config has per-bin locations, use those directly
        if (data.locations && Object.keys(data.locations).length > 0) {
            await apiPost('/api/bins/locations', { locations: data.locations });
        } else {
            configureBins();
        }

        addLog(`Loaded bin config: ${data.name}`);
        setTimeout(() => { loadBinLocations(); loadBinTable(); }, 300);
    } catch (e) {
        addLog('Failed to load bin config');
    }
}

async function deleteBinConfig() {
    const select = document.getElementById('bin-config-select');
    const filename = select.value;
    if (!filename) return;
    const name = select.selectedOptions[0]?.text || filename;
    if (!confirm(`Delete bin config "${name}"?`)) return;
    await fetch(`/api/bins/saved-configs/${filename}`, { method: 'DELETE' });
    addLog(`Deleted bin config: ${name}`);
    loadBinConfigList();
}

async function saveDefaultBinConfig() {
    await apiPost('/api/bins/saved-configs/_default.json', { name: 'Default' });
    addLog('Bin config saved as default (will auto-load on startup)');
}

async function applyManualBinPositions() {
    // Read bin X positions from the table inputs
    const inputs = document.querySelectorAll('.bin-x-input');
    const locations = {};
    inputs.forEach(input => {
        const binNum = input.id.replace('bin-x-', '');
        locations[binNum] = parseFloat(input.value);
    });

    // Read machine positions from table rows (staging, camera, width)
    const tblStagingX = document.getElementById('tbl-staging-x');
    const tblStagingW = document.getElementById('tbl-staging-w');
    const tblCamOffset = document.getElementById('tbl-cam-offset');

    const machinePayload = {};
    if (tblStagingX) machinePayload.staging_x = parseFloat(tblStagingX.value);
    if (tblStagingW) machinePayload.staging_width = parseFloat(tblStagingW.value);
    // Camera offset: compute absolute detection_x from source_x + offset
    const srcInput = document.getElementById('bin-x-0');
    const sourceX = srcInput ? parseFloat(srcInput.value) : _machinePositions.source_x;
    if (tblCamOffset) machinePayload.detection_x = sourceX + parseFloat(tblCamOffset.value);

    // The source bin X comes from bin 0 in the table
    if (srcInput) machinePayload.source_x = sourceX;

    await Promise.all([
        apiPost('/api/bins/locations', { locations }),
        apiPost('/api/bins/machine-positions', machinePayload),
    ]);

    addLog('Applied manual positions');
    loadBinLocations();
}

async function applyMachinePositions() {
    const source_x = parseFloat(document.getElementById('machine-source-x').value);
    const cam_offset = parseFloat(document.getElementById('machine-detect-x').value);
    const detection_x = source_x + cam_offset;
    const staging_x = parseFloat(document.getElementById('machine-staging-x').value);
    const staging_width = parseFloat(document.getElementById('machine-staging-w').value) || 200;
    await apiPost('/api/bins/machine-positions', { source_x, detection_x, staging_x, staging_width });
    addLog(`Machine positions: source=${source_x}, cam offset=${cam_offset}, staging=${staging_x}, width=${staging_width}`);
    loadBinLocations();
}

// =========================================================================
// Overflow chain configuration
// =========================================================================

async function loadOverflowConfig() {
    try {
        const data = await apiGet('/api/bins/overflow');
        const container = document.getElementById('overflow-chain-rows');
        container.innerHTML = '';
        const map = data.overflow_map || {};
        if (Object.keys(map).length > 0) {
            for (const [logicalBin, chain] of Object.entries(map)) {
                // Only show chains with overflow (more than just self)
                addOverflowRow(parseInt(logicalBin), chain.join(', '));
            }
        }
    } catch (e) {}
}

function addOverflowRow(logicalBin, chainStr) {
    const container = document.getElementById('overflow-chain-rows');
    const idx = container.children.length;
    const bin = logicalBin || (idx + 1);
    const chain = chainStr || '';
    const row = document.createElement('div');
    row.className = 'd-flex gap-2 mb-1 align-items-center';
    row.innerHTML = `
        <div class="input-group input-group-sm">
            <span class="input-group-text">Bin</span>
            <input type="number" class="form-control overflow-logical-bin" value="${bin}" min="1" max="20" style="max-width:70px;">
            <span class="input-group-text">Chain</span>
            <input type="text" class="form-control overflow-chain-bins" value="${chain}" placeholder="e.g. 1, 11, 12" title="Comma-separated physical bin numbers (first is primary)">
            <button class="btn btn-outline-danger btn-sm" onclick="this.closest('.d-flex').remove()">&times;</button>
        </div>`;
    container.appendChild(row);
}

async function saveOverflowConfig() {
    const rows = document.querySelectorAll('#overflow-chain-rows .d-flex');
    const overflowMap = {};
    for (const row of rows) {
        const logicalBin = parseInt(row.querySelector('.overflow-logical-bin').value);
        const chainStr = row.querySelector('.overflow-chain-bins').value.trim();
        if (chainStr && logicalBin) {
            const chain = chainStr.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n));
            if (chain.length > 0) {
                overflowMap[logicalBin] = chain;
            }
        }
    }
    const cardLimit = parseInt(document.getElementById('bin-card-limit').value) || 150;
    await apiPost('/api/bins/overflow', {
        overflow_map: Object.keys(overflowMap).length > 0 ? overflowMap : null,
        card_limit: cardLimit,
    });
    addLog(`Overflow config saved: ${Object.keys(overflowMap).length} chain(s)`);
    loadBinTable();
}

function clearOverflowConfig() {
    document.getElementById('overflow-chain-rows').innerHTML = '';
    apiPost('/api/bins/overflow', { overflow_map: null });
    addLog('Overflow chains cleared');
    loadBinTable();
}

// Overflow / fullness SocketIO events
socket.on('bin_full', (data) => {
    const reason = data.reason === 'probe' ? ' (detected by probe)' : '';
    addLog(`\u26A0\uFE0F BIN ${data.bin} IS FULL (${data.count}/${data.limit} cards)${reason}`);
    loadBinTable();
});

socket.on('bin_emptied', (data) => {
    addLog(`\u2705 Bin ${data.bin} marked empty (was ${data.old_count} cards)`);
    loadBinTable();
});

socket.on('bins_chain_full', (data) => {
    addLog(`\u{1F6D1} ALL BINS FULL for "${data.card_name}" (logical bin ${data.logical_bin}, chain [${data.chain.join(', ')}]). SORTING PAUSED — empty bins and resume.`);
    loadBinTable();
});

socket.on('overflow_map_updated', (data) => {
    loadBinTable();
});

// =========================================================================
// Sort configuration
// =========================================================================

// --- Sort config confirmation ---
// The user must click "Apply Sort Config" after configuring to
// confirm their choice. This prevents accidental mid-sort mode
// changes and makes the active sort mode explicit.
let _sortConfigApplied = false;
let _appliedSortMode = 'color';  // mode string that was last applied
let _appliedSortLabel = 'Color'; // human label for display

function applySortConfig() {
    const mode = document.getElementById('sort-mode').value;
    const label = document.getElementById('sort-mode').selectedOptions[0]?.text || mode;

    // Basic validation for custom modes
    if (mode === 'custom_file') {
        const file = document.getElementById('config-file-select').value;
        if (!file) {
            addLog('⚠️ Select a config file before applying');
            return;
        }
    }
    if (mode === 'custom_manual') {
        const count = parseInt(document.getElementById('custom-bin-count').value) || 7;
        let hasQuery = false;
        for (let i = 1; i <= count; i++) {
            const input = document.getElementById(`custom-query-${i}`);
            if (input && input.value.trim()) { hasQuery = true; break; }
        }
        if (!hasQuery) {
            addLog('⚠️ Enter at least one bin query before applying');
            return;
        }
    }

    _sortConfigApplied = true;
    _appliedSortMode = mode;
    _appliedSortLabel = label;

    const badge = document.getElementById('sort-config-status');
    badge.className = 'badge bg-success';
    badge.textContent = `✓ ${label}`;

    addLog(`Sort config applied: ${label}`);
}

function _markSortConfigDirty() {
    // Called when any sort config input changes — clears the "applied"
    // state so the user must re-confirm before starting a session.
    if (!_sortConfigApplied) return;
    _sortConfigApplied = false;
    const badge = document.getElementById('sort-config-status');
    if (badge) {
        badge.className = 'badge bg-warning text-dark';
        badge.textContent = 'Config changed — click Apply';
    }
}

function onSortModeChange() {
    const mode = document.getElementById('sort-mode').value;
    const filePanel = document.getElementById('custom-file-panel');
    const manualPanel = document.getElementById('custom-manual-panel');

    const setPanel = document.getElementById('set-config-panel');

    filePanel.style.display = mode === 'custom_file' ? '' : 'none';
    manualPanel.style.display = (mode === 'custom_manual' || mode === 'custom_file') ? '' : 'none';
    setPanel.style.display = mode === 'set' ? '' : 'none';

    if (mode === 'set') {
        loadSetConfig();
    }
    if (mode === 'custom_file') {
        loadConfigFileList();
    }
    if (mode === 'custom_manual') {
        buildCustomQueryInputs();
        loadCustomConfigFileList();
    }

    // Update simulation mode display
    const label = document.getElementById('sort-mode').selectedOptions[0]?.text || mode;
    const simDisplay = document.getElementById('sim-mode-display');
    if (simDisplay) simDisplay.textContent = label;

    // Sort mode changed — user must re-apply
    _markSortConfigDirty();
}

async function loadConfigFileList() {
    const data = await apiGet('/api/sort/configs');
    const select = document.getElementById('config-file-select');
    select.innerHTML = '<option value="">-- Select a config file --</option>';
    for (const cfg of data.configs || []) {
        select.innerHTML += `<option value="${cfg.filename}">${cfg.filename}</option>`;
    }
}

async function loadConfigFile() {
    const filename = document.getElementById('config-file-select').value;
    if (!filename) return;
    const data = await apiGet(`/api/sort/configs/${filename}`);
    document.getElementById('config-file-preview').textContent = data.content || '';
}

function buildCustomQueryInputs() {
    const count = parseInt(document.getElementById('custom-bin-count').value) || 10;
    const fbEl = document.getElementById('custom-fallback');
    let fallback = parseInt(fbEl.value) || count;
    // Clamp fallback into [1, count] so SortConfig validation doesn't
    // reject it. This fires when the user changes bin count or when the
    // hardware sync sets a lower count than the default 10.
    if (fallback > count) {
        fallback = count;
        fbEl.value = count;
    }
    if (fallback < 1) {
        fallback = 1;
        fbEl.value = 1;
    }
    const container = document.getElementById('custom-query-inputs');
    container.innerHTML = '';
    for (let i = 1; i <= count; i++) {
        const isFallback = i === fallback;
        const div = document.createElement('div');
        div.className = 'input-group input-group-sm mb-1';
        div.innerHTML = `
            <span class="input-group-text" style="width:60px;">Bin ${i}</span>
            <input type="text" class="form-control" id="custom-query-${i}"
                   placeholder="${isFallback ? '(fallback bin)' : 'Scryfall query (e.g. c:w t:creature)'}"
                   ${isFallback ? 'disabled' : ''}
                   oninput="_markSortConfigDirty()">
            <div class="input-group-text">
                <input type="checkbox" class="form-check-input mt-0 overflow-check"
                       id="custom-overflow-${i}" data-bin="${i}"
                       title="Use as overflow for another bin's query"
                       ${isFallback ? 'disabled' : ''}>
                <label class="form-check-label ms-1 small" for="custom-overflow-${i}">Overflow</label>
            </div>
        `;
        container.appendChild(div);
    }
    // Wire up overflow checkbox logic: when an overflow checkbox is
    // checked on a bin with no query, that bin becomes an overflow
    // target. When it's checked, disable its query input (it inherits
    // from its parent). Show a dropdown to select which bin it overflows.
    _wireOverflowCheckboxes(count, fallback);
}

function _wireOverflowCheckboxes(count, fallback) {
    for (let i = 1; i <= count; i++) {
        if (i === fallback) continue;
        const cb = document.getElementById(`custom-overflow-${i}`);
        if (!cb) continue;
        cb.addEventListener('change', () => {
            const input = document.getElementById(`custom-query-${i}`);
            if (cb.checked) {
                // Overflow bin: disable query field, show parent selector
                if (input) {
                    input.disabled = true;
                    input.value = '';
                    input.placeholder = '(overflow — select parent below)';
                }
                _showOverflowParentSelect(i, count, fallback);
            } else {
                // Regular bin: enable query field, remove parent selector
                if (input) {
                    input.disabled = false;
                    input.placeholder = 'Scryfall query (e.g. c:w t:creature)';
                }
                _removeOverflowParentSelect(i);
            }
        });
    }
}

function _showOverflowParentSelect(binNum, count, fallback) {
    // Remove any existing select
    _removeOverflowParentSelect(binNum);
    // Build list of bins that have queries (potential parents)
    const options = ['<option value="">-- overflow for --</option>'];
    for (let j = 1; j <= count; j++) {
        if (j === binNum || j === fallback) continue;
        const cb = document.getElementById(`custom-overflow-${j}`);
        if (cb && cb.checked) continue; // another overflow bin can't be a parent
        const input = document.getElementById(`custom-query-${j}`);
        const query = input ? input.value.trim() : '';
        if (query) {
            const short = query.length > 25 ? query.substring(0, 25) + '…' : query;
            options.push(`<option value="${j}">Bin ${j}: ${short}</option>`);
        }
    }
    const sel = document.createElement('select');
    sel.className = 'form-select form-select-sm';
    sel.id = `overflow-parent-${binNum}`;
    sel.style.maxWidth = '200px';
    sel.innerHTML = options.join('');
    // Insert after the input-group
    const container = document.getElementById(`custom-query-${binNum}`)?.closest('.input-group');
    if (container && container.parentElement) {
        const wrapper = document.createElement('div');
        wrapper.className = 'ms-2 d-inline-block';
        wrapper.id = `overflow-parent-wrapper-${binNum}`;
        wrapper.appendChild(sel);
        container.parentElement.insertBefore(wrapper, container.nextSibling);
    }
}

function _removeOverflowParentSelect(binNum) {
    const wrapper = document.getElementById(`overflow-parent-wrapper-${binNum}`);
    if (wrapper) wrapper.remove();
}

async function loadCustomConfigFileList() {
    try {
        const data = await apiGet('/api/sort/configs');
        const select = document.getElementById('custom-load-file');
        if (!select) return;
        select.innerHTML = '<option value="">-- Load from file --</option>';
        for (const cfg of data.configs || []) {
            select.innerHTML += `<option value="${cfg.filename}">${cfg.filename}</option>`;
        }
    } catch (e) {}
}

async function loadCustomConfigFromFile() {
    const select = document.getElementById('custom-load-file');
    const filename = select ? select.value : '';
    if (!filename) return;
    try {
        const data = await apiGet(`/api/sort/configs/${filename}`);
        const content = data.content || '';
        // Parse the config file format:
        //   bins: N
        //   fallback: N
        //   limit: N
        //   bin1: query
        //   bin2: query
        const lines = content.split('\n');
        let binCount = 10, fallback = 10, limit = '';
        const queries = {};
        for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;
            const match = trimmed.match(/^(\w+):\s*(.*)$/);
            if (!match) continue;
            const key = match[1].toLowerCase();
            const val = match[2].trim();
            if (key === 'bins') binCount = parseInt(val) || 10;
            else if (key === 'fallback') fallback = parseInt(val) || 10;
            else if (key === 'limit') limit = val;
            else if (key.startsWith('bin')) {
                const num = parseInt(key.replace('bin', ''));
                if (!isNaN(num)) queries[num] = val;
            }
        }
        // Apply to UI
        document.getElementById('custom-bin-count').value = binCount;
        document.getElementById('custom-fallback').value = fallback;
        document.getElementById('custom-bin-limit').value = limit;
        buildCustomQueryInputs();
        // Fill in queries
        for (const [num, query] of Object.entries(queries)) {
            const input = document.getElementById(`custom-query-${num}`);
            if (input) input.value = query;
        }
        addLog(`Loaded custom config: ${filename}`);
    } catch (e) {
        addLog(`Failed to load config: ${filename}`);
    }
}

async function saveCustomConfig() {
    const filename = prompt('Save as filename (without .txt):');
    if (!filename) return;
    const count = parseInt(document.getElementById('custom-bin-count').value) || 10;
    const fallback = parseInt(document.getElementById('custom-fallback').value) || count;
    const limit = document.getElementById('custom-bin-limit').value;

    let content = `bins: ${count}\nfallback: ${fallback}\n`;
    if (limit) content += `limit: ${limit}\n`;

    for (let i = 1; i <= count; i++) {
        const input = document.getElementById(`custom-query-${i}`);
        if (input && input.value.trim()) {
            content += `bin${i}: ${input.value.trim()}\n`;
        }
    }

    await apiPost(`/api/sort/configs/${filename}`, { content });
    addLog(`Saved config: ${filename}.txt`);
    loadConfigFileList();
    loadCustomConfigFileList();
}

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

function startSession() {
    // Require the user to explicitly apply the sort config before
    // starting. This prevents starting a session with an un-confirmed
    // or stale config.
    if (!_sortConfigApplied) {
        addLog('⚠️ Click "Apply Sort Config" first to confirm your sort mode');
        const badge = document.getElementById('sort-config-status');
        if (badge) {
            badge.className = 'badge bg-danger';
            badge.textContent = 'Apply config first!';
        }
        // Open the sort config panel if it's collapsed
        const collapse = document.getElementById('sort-config-collapse');
        if (collapse && !collapse.classList.contains('show')) {
            new bootstrap.Collapse(collapse, { toggle: true });
        }
        return;
    }

    const mode = _appliedSortMode;
    let payload = { mode };

    if (mode === 'custom_file') {
        payload.config_file = document.getElementById('config-file-select').value;
    } else if (mode === 'custom_manual') {
        const count = parseInt(document.getElementById('custom-bin-count').value) || 7;
        const fallback = parseInt(document.getElementById('custom-fallback').value) || count;
        const limit = document.getElementById('custom-bin-limit').value;
        const queries = {};
        // Build overflow map: for each overflow-checked bin, find its
        // parent bin and build a chain parent -> [parent, overflow1, overflow2, ...]
        const overflowMap = {};
        for (let i = 1; i <= count; i++) {
            const cb = document.getElementById(`custom-overflow-${i}`);
            const input = document.getElementById(`custom-query-${i}`);
            if (cb && cb.checked) {
                // This bin is an overflow target — find its parent
                const parentSel = document.getElementById(`overflow-parent-${i}`);
                const parentBin = parentSel ? parseInt(parentSel.value) : null;
                if (parentBin) {
                    if (!overflowMap[parentBin]) {
                        overflowMap[parentBin] = [parentBin];
                    }
                    overflowMap[parentBin].push(i);
                }
            } else if (input && input.value.trim()) {
                queries[i] = input.value.trim();
            }
        }
        payload.custom_queries = { bin_count: count, fallback_bin: fallback, queries };
        if (limit) payload.custom_queries.bin_limit = parseInt(limit);
        // Send overflow map if any overflow bins were configured
        if (Object.keys(overflowMap).length > 0) {
            payload.overflow_map = overflowMap;
        }
    }

    const notes = document.getElementById('session-notes')?.value?.trim();
    if (notes) payload.notes = notes;
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
                html += `<div class="bin-card-entry small text-truncate ps-2" title="${card.name} (${card.set}/${card.collector_number})">
                    <span class="text-muted">#${card.scan_num}</span> ${card.name}
                    <span class="text-muted">${card.set ? card.set.toUpperCase() : ''}</span>
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
// Camera feed helpers
// =========================================================================

const _feedIntervals = {};

function startCameraFeed(imgId) {
    const img = document.getElementById(imgId);
    if (!img) return;
    // Start camera on backend
    apiPost('/api/camera/start');
    // Set MJPEG source
    img.src = '/api/camera/feed?' + Date.now();
    img.style.display = '';
}

function stopCameraFeed(imgId) {
    const img = document.getElementById(imgId);
    if (!img) return;
    img.src = '';
}

// =========================================================================
// Simulation
// =========================================================================

let _simPollInterval = null;

function startSimulation() {
    const count = parseInt(document.getElementById('sim-card-count').value) || 10;
    // Use the sort mode from the Sort Session tab's Sort Configuration panel
    const mode = document.getElementById('sort-mode').value;
    const payload = { card_count: count, mode };

    // Include custom config if applicable
    if (mode === 'custom_file') {
        payload.config_file = document.getElementById('config-file-select').value;
    } else if (mode === 'custom_manual') {
        const binCount = parseInt(document.getElementById('custom-bin-count').value) || 10;
        const fallback = parseInt(document.getElementById('custom-fallback').value) || binCount;
        const limit = document.getElementById('custom-bin-limit').value;
        const queries = {};
        for (let i = 1; i <= binCount; i++) {
            const input = document.getElementById(`custom-query-${i}`);
            if (input && input.value.trim()) {
                queries[i] = input.value.trim();
            }
        }
        payload.custom_queries = { bin_count: binCount, fallback_bin: fallback, queries };
        if (limit) payload.custom_queries.bin_limit = parseInt(limit);
    }

    apiPost('/api/sim/test-run', payload);
    document.getElementById('sim-results').innerHTML = '<p class="text-muted small">Running...</p>';
    document.getElementById('sim-progress').textContent = `0/${count} cards sorted`;
    // Start polling as fallback for SocketIO events
    _startSimPolling();
}

function _startSimPolling() {
    _stopSimPolling();
    let _simSawRunning = false;
    _simPollInterval = setInterval(async () => {
        try {
            const data = await apiGet('/api/sim/status');
            if (data.running) _simSawRunning = true;
            // Update progress
            if (data.progress > 0) {
                document.getElementById('sim-progress').textContent =
                    `${data.progress}/${data.total} cards sorted`;
            } else if (data.running) {
                document.getElementById('sim-progress').textContent = 'Loading card data...';
            }
            // Update bin counts on canvas (deferred if animating)
            if (data.bin_counts && Object.keys(data.bin_counts).length > 0) {
                const newCounts = {};
                for (const [k, v] of Object.entries(data.bin_counts)) {
                    newCounts[k] = v;
                }
                if (_motionWaypoints.length > 0) {
                    _pendingBinUpdates.push({ cards_per_bin: newCounts });
                } else {
                    _cachedBinCounts = newCounts;
                    drawMotionCanvas();
                }
            }
            // If simulation finished (must have seen it running first), show results
            if (!data.running && _simSawRunning) {
                _stopSimPolling();
                document.getElementById('sim-progress').textContent = `Complete: ${data.progress} cards`;
                _renderSimResults(data.results || []);
            }
        } catch (e) {
            // Ignore poll errors
        }
    }, 1000);
}

function _stopSimPolling() {
    if (_simPollInterval) {
        clearInterval(_simPollInterval);
        _simPollInterval = null;
    }
}

function _renderSimResults(results) {
    const panel = document.getElementById('sim-results');
    if (!results.length) {
        panel.innerHTML = '<p class="text-muted small">No results</p>';
        return;
    }
    let html = '<table class="table table-sm"><thead><tr><th>Card</th><th>Set</th><th>Bin</th></tr></thead><tbody>';
    for (const r of results) {
        html += `<tr><td>${r.card_name}</td><td>${r.set}</td><td>${r.bin}</td></tr>`;
    }
    html += '</tbody></table>';
    panel.innerHTML = html;
}

socket.on('sim_progress', (data) => {
    document.getElementById('sim-progress').textContent =
        `${data.progress}/${data.total} cards sorted`;
    // Defer bin count updates when motion animation is playing
    const newCounts = {};
    for (const [k, v] of Object.entries(data.bin_contents)) {
        newCounts[k] = v;
    }
    if (_motionWaypoints.length > 0) {
        _pendingBinUpdates.push({ cards_per_bin: newCounts });
    } else {
        _cachedBinCounts = newCounts;
        drawMotionCanvas();
    }
});

socket.on('sim_card_sorted', (data) => {
    // Could append to results list in real-time
});

socket.on('sim_complete', (data) => {
    _stopSimPolling();
    document.getElementById('sim-progress').textContent = `Complete: ${data.total} cards`;
    _renderSimResults(data.results || []);
});

// =========================================================================
// Collection
// =========================================================================

let _inventoryPage = 1;
let _inventorySort = 'name';
let _inventorySortDir = 'ASC';

async function loadCollectionStats() {
    try {
        const data = await apiGet('/api/collection/stats');
        document.getElementById('collection-stats').innerHTML = `
            <p class="mb-1">Unique cards: <strong>${data.unique_cards}</strong></p>
            <p class="mb-1">Total cards: <strong>${data.total_cards}</strong></p>
            <p class="mb-0">Total value: <strong>$${(data.total_value || 0).toFixed(2)}</strong></p>
        `;
    } catch (e) {
        document.getElementById('collection-stats').innerHTML = '<p class="text-muted">No database found</p>';
    }
}

async function loadBoxSummary() {
    try {
        const data = await apiGet('/api/collection/boxes/summary');
        const card = document.getElementById('box-summary-card');
        const body = document.getElementById('box-summary-body');
        if (!data.boxes || data.boxes.length === 0) {
            card.style.display = 'none';
            return;
        }
        card.style.display = 'block';
        let html = '<table class="table table-sm mb-0"><tbody>';
        for (const b of data.boxes) {
            const name = b.box_name === '__unassigned__' ? '<em>Unassigned</em>' : b.box_name;
            const val = b.total_value > 0 ? ` · $${b.total_value.toFixed(2)}` : '';
            html += `<tr><td class="small">${name}</td><td class="small text-end text-nowrap">${b.total_cards} cards${val}</td></tr>`;
        }
        html += '</tbody></table>';
        body.innerHTML = html;
    } catch (e) {}
}

function _priceClass(price) {
    if (!price) return '';
    if (price >= 50) return 'price-mythic';
    if (price >= 20) return 'price-rare';
    if (price >= 5) return 'price-uncommon';
    return '';
}

function _renderInventoryRow(item) {
    const price = item.price_usd ? `$${item.price_usd.toFixed(2)}` : 'N/A';
    const priceClass = _priceClass(item.price_usd);
    const boxDisplay = item.box || '<span class="text-secondary">—</span>';
    const removeBtns = item.quantity > 1
        ? `<button class="btn btn-outline-warning btn-sm py-0 px-1" onclick="deleteInventoryItem(${item.id}, 1)" title="Remove 1 copy">&minus;1</button>
           <button class="btn btn-outline-danger btn-sm py-0 px-1" onclick="deleteInventoryItem(${item.id})" title="Remove all copies">&times;</button>`
        : `<button class="btn btn-outline-danger btn-sm py-0 px-1" onclick="deleteInventoryItem(${item.id})" title="Remove">&times;</button>`;
    const imgUrl = item.set_code && item.collector_number
        ? _scryfallImageUrl(item.set_code, item.collector_number, 'normal')
        : '';
    const hoverAttr = imgUrl ? `onmouseenter="_showCardPreview(event, '${imgUrl}')" onmouseleave="_hideCardPreview()"` : '';
    const scryfallLink = item.set_code && item.collector_number
        ? `<a href="https://scryfall.com/card/${item.set_code}/${item.collector_number}" target="_blank" rel="noopener" class="text-decoration-none" title="View on Scryfall">${item.set_code.toUpperCase()}</a>`
        : item.set_code;
    return `<tr class="${priceClass}">
        <td><input type="checkbox" class="inv-select" value="${item.id}" onchange="_updateBulkCount()"></td>
        <td class="card-hover-name" style="cursor:default" ${hoverAttr}>${item.name}</td>
        <td>${scryfallLink}</td><td>${item.colors || ''}</td>
        <td class="small">${item.type_line || ''}</td><td>${item.rarity || ''}</td>
        <td>${price}</td><td>${item.quantity}</td>
        <td><span class="box-label" style="cursor:pointer" onclick="showBoxAssign(${item.id}, ${item.quantity})" title="Click to assign box">${boxDisplay}</span></td>
        <td class="text-nowrap">
            <button class="btn btn-outline-success btn-sm py-0 px-1" onclick="incrementInventoryItem(${item.id})" title="Add 1 copy">+1</button>
            ${removeBtns}
        </td>
    </tr>`;
}

async function loadInventory(page) {
    _inventoryPage = page || 1;
    try {
        const data = await apiGet(`/api/collection/inventory?page=${_inventoryPage}&per_page=50&sort=${_inventorySort}&dir=${_inventorySortDir}`);
        document.getElementById('inventory-count').textContent = data.total;
        const tbody = document.getElementById('inventory-body');
        tbody.innerHTML = '';
        for (const item of data.items || []) {
            tbody.innerHTML += _renderInventoryRow(item);
        }
        _updateSortArrows();
        // Pagination
        const pagDiv = document.getElementById('inventory-pagination');
        pagDiv.innerHTML = '';
        if (data.pages > 1) {
            for (let p = 1; p <= data.pages; p++) {
                const btn = document.createElement('button');
                btn.className = `btn btn-sm ${p === data.page ? 'btn-primary' : 'btn-outline-secondary'} mx-1`;
                btn.textContent = p;
                btn.onclick = () => loadInventory(p);
                pagDiv.appendChild(btn);
            }
        }
    } catch (e) {}
}

function _updateSortArrows() {
    document.querySelectorAll('#tab-collection th.sortable').forEach(th => {
        const arrow = th.querySelector('.sort-arrow');
        if (th.dataset.sort === _inventorySort) {
            arrow.textContent = _inventorySortDir === 'ASC' ? ' \u25B2' : ' \u25BC';
        } else {
            arrow.textContent = '';
        }
    });
}

function sortInventory(column) {
    if (_inventorySort === column) {
        _inventorySortDir = _inventorySortDir === 'ASC' ? 'DESC' : 'ASC';
    } else {
        _inventorySort = column;
        _inventorySortDir = 'ASC';
    }
    loadInventory(1);
}

async function searchCollection() {
    const params = new URLSearchParams();
    const name = document.getElementById('search-name').value;
    const set = document.getElementById('search-set').value;
    const colors = document.getElementById('search-colors').value;
    const rarity = document.getElementById('search-rarity').value;
    const box = document.getElementById('search-box').value;
    if (name) params.set('name', name);
    if (set) params.set('set', set);
    if (colors) params.set('colors', colors);
    if (rarity) params.set('rarity', rarity);
    if (box) params.set('box', box);
    params.set('sort', _inventorySort);
    params.set('dir', _inventorySortDir);

    try {
        const data = await apiGet(`/api/collection/inventory?${params.toString()}`);
        document.getElementById('inventory-count').textContent = data.total;
        const tbody = document.getElementById('inventory-body');
        tbody.innerHTML = '';
        for (const item of data.items || []) {
            tbody.innerHTML += _renderInventoryRow(item);
        }
        _updateSortArrows();
    } catch (e) {}
}

async function incrementInventoryItem(itemId) {
    await fetch(`/api/collection/inventory/${itemId}/increment`, { method: 'POST' });
    loadInventory(_inventoryPage);
    loadCollectionStats();
}

async function deleteInventoryItem(itemId, qty) {
    if (!qty && !confirm('Remove all copies of this card?')) return;
    const url = qty
        ? `/api/collection/inventory/${itemId}?qty=${qty}`
        : `/api/collection/inventory/${itemId}`;
    await fetch(url, { method: 'DELETE' });
    loadInventory(_inventoryPage);
    loadCollectionStats();
}

// ---- Add Card with autocomplete ----

let _addCardDebounce = null;
let _addCardPrintings = [];  // cached printings for currently selected card

function _initAddCardAutocomplete() {
    const input = document.getElementById('add-card-name');
    const sugBox = document.getElementById('add-card-suggestions');

    input.addEventListener('input', () => {
        clearTimeout(_addCardDebounce);
        const q = input.value.trim();
        if (q.length < 2) { sugBox.style.display = 'none'; return; }
        _addCardDebounce = setTimeout(async () => {
            try {
                const data = await apiGet(`/api/cards/autocomplete?q=${encodeURIComponent(q)}`);
                if (!data.suggestions || data.suggestions.length === 0) {
                    sugBox.style.display = 'none';
                    return;
                }
                sugBox.innerHTML = data.suggestions.map(name =>
                    `<button type="button" class="list-group-item list-group-item-action py-1 px-2 small"
                        onclick="_selectCardName('${name.replace(/'/g, "\\'")}')">${name}</button>`
                ).join('');
                sugBox.style.display = 'block';
            } catch (e) { sugBox.style.display = 'none'; }
        }, 250);
    });

    // Hide suggestions when clicking outside
    document.addEventListener('click', (e) => {
        if (!input.contains(e.target) && !sugBox.contains(e.target)) {
            sugBox.style.display = 'none';
        }
    });

    // Update printing info when set changes
    document.getElementById('add-card-set').addEventListener('change', _updatePrintingInfo);
}

async function _selectCardName(name) {
    const input = document.getElementById('add-card-name');
    const sugBox = document.getElementById('add-card-suggestions');
    const setSelect = document.getElementById('add-card-set');

    input.value = name;
    sugBox.style.display = 'none';

    // Fetch printings for this card
    try {
        const data = await apiGet(`/api/cards/printings?name=${encodeURIComponent(name)}`);
        _addCardPrintings = data.printings || [];
        setSelect.innerHTML = '';
        if (_addCardPrintings.length === 0) {
            setSelect.innerHTML = '<option value="">No printings found</option>';
            setSelect.disabled = true;
        } else {
            for (const p of _addCardPrintings) {
                const priceStr = p.price_usd ? ` — $${p.price_usd.toFixed(2)}` : '';
                const opt = document.createElement('option');
                opt.value = `${p.set}|${p.collector_number}`;
                opt.textContent = `${p.set.toUpperCase()} #${p.collector_number} (${p.rarity})${priceStr}`;
                setSelect.appendChild(opt);
            }
            setSelect.disabled = false;
        }
        _updatePrintingInfo();
    } catch (e) {
        setSelect.innerHTML = '<option value="">Error loading printings</option>';
        setSelect.disabled = true;
    }
}

function _scryfallImageUrl(set, collectorNumber, version) {
    return `https://api.scryfall.com/cards/${encodeURIComponent(set)}/${encodeURIComponent(collectorNumber)}?format=image&version=${version || 'normal'}`;
}

function _showCardPreview(event, imgUrl) {
    let el = document.getElementById('card-hover-preview');
    if (!el) {
        el = document.createElement('div');
        el.id = 'card-hover-preview';
        el.style.cssText = 'position:fixed;z-index:9999;pointer-events:none;display:none;filter:drop-shadow(0 4px 12px rgba(0,0,0,0.7));transition:opacity 0.15s';
        el.innerHTML = '<img style="max-height:340px;border-radius:12px">';
        document.body.appendChild(el);
    }
    const img = el.querySelector('img');
    img.src = imgUrl;
    el.style.display = 'block';
    el.style.opacity = '0';
    img.onload = () => { el.style.opacity = '1'; };

    // Position to the right edge of the viewport, vertically near cursor
    // This avoids covering name, price, or any table content
    const y = event.clientY - 100;
    const maxY = window.innerHeight - 370;
    el.style.left = (window.innerWidth - 270) + 'px';
    el.style.top = Math.max(8, Math.min(y, maxY)) + 'px';

    // Highlight the row
    const row = event.target.closest('tr');
    if (row) row.classList.add('inventory-row-hover');
}

function _hideCardPreview() {
    const el = document.getElementById('card-hover-preview');
    if (el) {
        el.style.display = 'none';
        el.querySelector('img').src = '';
    }
    // Remove highlight from all rows
    document.querySelectorAll('.inventory-row-hover').forEach(r => r.classList.remove('inventory-row-hover'));
}

function _updatePrintingInfo() {
    const infoEl = document.getElementById('add-card-printing-info');
    const imageEl = document.getElementById('add-card-image');
    const imgTag = document.getElementById('add-card-img');
    const val = document.getElementById('add-card-set').value;
    if (!val || _addCardPrintings.length === 0) {
        infoEl.style.display = 'none';
        imageEl.style.display = 'none';
        return;
    }
    const [set, num] = val.split('|');
    const p = _addCardPrintings.find(x => x.set === set && x.collector_number === num);
    if (p) {
        infoEl.style.display = 'block';
        infoEl.textContent = `${p.type_line || ''} · ${p.colors || 'Colorless'}`;
        imgTag.src = _scryfallImageUrl(set, num, 'normal');
        imgTag.alt = document.getElementById('add-card-name').value;
        imageEl.style.display = 'block';
    } else {
        infoEl.style.display = 'none';
        imageEl.style.display = 'none';
    }
}

async function addManualCard() {
    const name = document.getElementById('add-card-name').value.trim();
    const setVal = document.getElementById('add-card-set').value;
    if (!name) {
        alert('Select a card name from the suggestions.');
        return;
    }
    if (!setVal) {
        alert('Select a set/printing.');
        return;
    }

    const [set_code, collector_number] = setVal.split('|');
    const printing = _addCardPrintings.find(
        p => p.set === set_code && p.collector_number === collector_number
    );

    const statusEl = document.getElementById('add-card-status');
    statusEl.style.display = 'block';
    statusEl.className = 'small mt-1 text-info';
    statusEl.textContent = 'Adding...';

    try {
        const resp = await fetch('/api/collection/inventory', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name,
                set_code,
                collector_number: collector_number || '',
                colors: printing?.colors || null,
                type_line: printing?.type_line || null,
                rarity: printing?.rarity || null,
                price_usd: printing?.price_usd || null,
                quantity: parseInt(document.getElementById('add-card-qty').value) || 1,
            }),
        });
        const data = await resp.json();
        if (resp.ok) {
            statusEl.className = 'small mt-1 text-success';
            statusEl.textContent = `Added "${name}" (${set_code.toUpperCase()}) to collection`;
            document.getElementById('add-card-name').value = '';
            document.getElementById('add-card-set').innerHTML = '<option value="">Select a card first</option>';
            document.getElementById('add-card-set').disabled = true;
            document.getElementById('add-card-printing-info').style.display = 'none';
            document.getElementById('add-card-image').style.display = 'none';
            document.getElementById('add-card-qty').value = '1';
            _addCardPrintings = [];
            loadInventory(_inventoryPage);
            loadCollectionStats();
        } else {
            statusEl.className = 'small mt-1 text-danger';
            statusEl.textContent = data.error || 'Failed to add card';
        }
    } catch (e) {
        statusEl.className = 'small mt-1 text-danger';
        statusEl.textContent = 'Failed: ' + e.message;
    }
}

async function deleteSession(sessionId) {
    if (!confirm(`Delete session #${sessionId} and all its scan history?`)) return;
    await fetch(`/api/collection/sessions/${sessionId}`, { method: 'DELETE' });
    loadSessionHistory();
    loadCollectionStats();
}

async function resetCollection() {
    if (!confirm('This will permanently delete ALL cards, sessions, and scan history. Are you sure?')) return;
    if (!confirm('This cannot be undone. Click OK to confirm.')) return;
    try {
        await apiPost('/api/collection/reset');
        loadCollectionStats();
        loadInventory();
        loadSessionHistory();
        loadBoxList();
        addLog('Collection reset — all data deleted');
    } catch (e) {
        addLog('Failed to reset collection: ' + e.message);
    }
}

async function importCollectionCSV(input) {
    if (!input.files || !input.files[0]) return;
    const file = input.files[0];
    const statusEl = document.getElementById('import-status');
    statusEl.style.display = 'block';
    statusEl.className = 'small mt-1 text-info';
    statusEl.textContent = `Importing ${file.name}...`;

    const formData = new FormData();
    formData.append('file', file);

    try {
        const resp = await fetch('/api/collection/import/csv', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();
        if (resp.ok) {
            statusEl.className = 'small mt-1 text-success';
            statusEl.textContent = `Imported: ${data.imported} new, ${data.updated} updated, ${data.skipped} skipped`;
            loadCollectionStats();
            loadInventory();
            addLog(`CSV imported: ${data.total} cards processed`);
        } else {
            statusEl.className = 'small mt-1 text-danger';
            statusEl.textContent = data.error || 'Import failed';
        }
    } catch (e) {
        statusEl.className = 'small mt-1 text-danger';
        statusEl.textContent = 'Import failed: ' + e.message;
    }
    // Reset file input so the same file can be re-imported
    input.value = '';
}

async function generateTestCollection() {
    const count = parseInt(document.getElementById('test-collection-count').value) || 50;
    const statusEl = document.getElementById('import-status');
    statusEl.style.display = 'block';
    statusEl.className = 'small mt-1 text-info';
    statusEl.textContent = `Generating ${count} test cards...`;

    try {
        const resp = await fetch('/api/collection/generate-test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ count }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            statusEl.className = 'small mt-1 text-danger';
            statusEl.textContent = data.error || 'Generation failed';
            return;
        }
        statusEl.className = 'small mt-1 text-success';
        statusEl.textContent = `Added ${data.added} random cards to collection`;
        loadCollectionStats();
        loadInventory();
        loadBoxList();
        addLog(`Test collection: ${data.added} random cards generated`);
    } catch (e) {
        statusEl.className = 'small mt-1 text-danger';
        statusEl.textContent = 'Generation failed — ensure Scryfall data is loaded';
    }
}

// ---- Bulk selection ----

function toggleSelectAll(master) {
    document.querySelectorAll('.inv-select').forEach(cb => { cb.checked = master.checked; });
    _updateBulkCount();
}

function _updateBulkCount() {
    const checked = document.querySelectorAll('.inv-select:checked').length;
    const bar = document.getElementById('bulk-actions');
    bar.style.display = checked > 0 ? 'flex' : 'none';
    document.getElementById('bulk-count').textContent = checked;
}

function bulkClearSelection() {
    document.querySelectorAll('.inv-select').forEach(cb => { cb.checked = false; });
    document.getElementById('select-all-inv').checked = false;
    _updateBulkCount();
}

async function bulkBoxAssign() {
    const ids = [...document.querySelectorAll('.inv-select:checked')].map(cb => parseInt(cb.value));
    if (ids.length === 0) return;

    // Reuse the box assign overlay but for bulk
    let boxes = [];
    try { const data = await apiGet('/api/collection/boxes'); boxes = data.boxes || []; } catch (e) {}
    const boxListHtml = boxes.map(b => `<option value="${b}">`).join('');

    const overlay = document.createElement('div');
    overlay.id = 'box-assign-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;z-index:9999';
    overlay.innerHTML = `
        <div style="background:var(--bg-card,#1c1f26);border:1px solid var(--border,#2d3240);border-radius:8px;padding:20px;min-width:320px;color:var(--text-primary,#e8eaf0)">
            <h6>Assign ${ids.length} cards to Box</h6>
            <input type="text" class="form-control form-control-sm mb-2" id="bulk-box-name"
                   placeholder="Box name" list="bulk-box-list" autocomplete="off">
            <datalist id="bulk-box-list">${boxListHtml}</datalist>
            <div class="d-flex gap-2">
                <button class="btn btn-primary btn-sm" id="bulk-box-confirm">Assign</button>
                <button class="btn btn-outline-secondary btn-sm" id="bulk-box-remove">Remove from box</button>
                <button class="btn btn-outline-secondary btn-sm" onclick="document.getElementById('box-assign-overlay').remove()">Cancel</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

    const doAssign = async (boxName) => {
        for (const id of ids) {
            await fetch(`/api/collection/inventory/${id}/box`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ box: boxName }),
            });
        }
        overlay.remove();
        bulkClearSelection();
        loadInventory(_inventoryPage);
        loadBoxList();
        loadBoxSummary();
    };

    document.getElementById('bulk-box-confirm').onclick = () => {
        const name = document.getElementById('bulk-box-name').value.trim();
        if (!name) { alert('Enter a box name'); return; }
        doAssign(name);
    };
    document.getElementById('bulk-box-remove').onclick = () => doAssign('');
    document.getElementById('bulk-box-name').focus();
}

// ---- Box management ----

async function loadBoxList() {
    try {
        const data = await apiGet('/api/collection/boxes');
        const sel = document.getElementById('search-box');
        // Keep first two options (Any box, Unassigned)
        while (sel.options.length > 2) sel.remove(2);
        for (const box of data.boxes || []) {
            const opt = document.createElement('option');
            opt.value = box;
            opt.textContent = box;
            sel.appendChild(opt);
        }
    } catch (e) {}
}

async function showBoxAssign(itemId, quantity) {
    // Fetch current box list for datalist
    let boxes = [];
    try {
        const data = await apiGet('/api/collection/boxes');
        boxes = data.boxes || [];
    } catch (e) {}

    const boxListHtml = boxes.map(b => `<option value="${b}">`).join('');

    // Build a small inline form — use a modal-like overlay
    const overlay = document.createElement('div');
    overlay.id = 'box-assign-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;z-index:9999';
    overlay.innerHTML = `
        <div style="background:var(--bg-card,#1c1f26);border:1px solid var(--border,#2d3240);border-radius:8px;padding:20px;min-width:320px;color:var(--text-primary,#e8eaf0)">
            <h6>Assign to Box</h6>
            <input type="text" class="form-control form-control-sm mb-2" id="box-assign-name"
                   placeholder="Box name (e.g. Red Box, Trade Binder)" list="box-assign-list" autocomplete="off">
            <datalist id="box-assign-list">${boxListHtml}</datalist>
            ${quantity > 1 ? `
            <label class="form-label small">Move how many? (total: ${quantity})</label>
            <input type="number" class="form-control form-control-sm mb-2" id="box-assign-qty"
                   value="${quantity}" min="1" max="${quantity}">
            ` : ''}
            <div class="d-flex gap-2">
                <button class="btn btn-primary btn-sm" onclick="confirmBoxAssign(${itemId}, ${quantity})">Assign</button>
                <button class="btn btn-outline-secondary btn-sm" onclick="confirmBoxAssign(${itemId}, ${quantity}, true)">Remove from box</button>
                <button class="btn btn-outline-secondary btn-sm" onclick="document.getElementById('box-assign-overlay').remove()">Cancel</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    document.getElementById('box-assign-name').focus();
}

async function confirmBoxAssign(itemId, totalQty, removeBox) {
    const overlay = document.getElementById('box-assign-overlay');
    const boxName = removeBox ? '' : (document.getElementById('box-assign-name')?.value || '').trim();
    const qtyEl = document.getElementById('box-assign-qty');
    const moveQty = qtyEl ? parseInt(qtyEl.value) : null;

    if (!removeBox && !boxName) {
        alert('Enter a box name or click "Remove from box"');
        return;
    }

    const body = { box: boxName };
    if (moveQty !== null && moveQty < totalQty) {
        body.quantity = moveQty;
    }

    await fetch(`/api/collection/inventory/${itemId}/box`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });

    if (overlay) overlay.remove();
    loadInventory(_inventoryPage);
    loadBoxList();
}

async function loadSessionHistory() {
    try {
        const data = await apiGet('/api/collection/sessions');
        const tbody = document.getElementById('sessions-body');
        tbody.innerHTML = '';
        for (const s of data.sessions || []) {
            tbody.innerHTML += `<tr>
                <td>${s.id}</td>
                <td>${s.start_time || '?'}</td>
                <td>${s.sort_mode || '?'}</td>
                <td class="small">${s.notes || ''}</td>
                <td>${s.total_scans || 0}</td>
                <td>${s.recognized || 0}</td>
                <td>${s.unrecognized || 0}</td>
                <td><button class="btn btn-outline-danger btn-sm py-0 px-1" onclick="deleteSession(${s.id})" title="Delete session">&times;</button></td>
            </tr>`;
        }
    } catch (e) {}
}

// =========================================================================
// Database management
// =========================================================================

async function loadDbInfo() {
    try {
        const data = await apiGet('/api/database/status');
        const panel = document.getElementById('db-info');
        panel.innerHTML = `
            <p class="mb-1">Cards JSON: <strong>${data.cards_json || '?'}</strong> (${data.cards_json_size_mb || 0} MB)</p>
            <p class="mb-1">Card count: <strong>${data.card_count || 0}</strong></p>
            <p class="mb-1">Hash DB v1: ${data.hash_db_v1_exists ? `${data.hash_db_v1_count || '?'} entries (${data.hash_db_v1_size_mb || 0} MB)` : '<span class="text-danger">Not found</span>'}</p>
            <p class="mb-1">Hash DB v2: ${data.hash_db_v2_exists ? `${data.hash_db_v2_count || '?'} entries (${data.hash_db_v2_size_mb || 0} MB)` : '<span class="text-danger">Not found</span>'}</p>
            <p class="mb-0">Card images: <strong>${data.image_count || 0}</strong> (~${data.images_size_gb || 0} GB)</p>
        `;
    } catch (e) {}
}

async function checkDbUpdate() {
    const panel = document.getElementById('db-update-check');
    panel.textContent = 'Checking...';
    try {
        const data = await apiPost('/api/database/check-update');
        if (data.needs_update) {
            panel.innerHTML = `<span class="text-warning">Update available!</span><br>
                Current: ${data.current_file}<br>
                Available: ${data.remote_file}<br>
                Updated: ${data.updated_at}`;
        } else if (data.available) {
            panel.innerHTML = '<span class="text-success">Database is up to date.</span>';
        } else {
            panel.innerHTML = `<span class="text-danger">Error: ${data.error}</span>`;
        }
    } catch (e) {
        panel.textContent = 'Error checking for updates';
    }
}

socket.on('db_update_progress', (data) => {
    const bar = document.getElementById('db-progress-bar');
    const msg = document.getElementById('db-progress-message');
    const container = bar.parentElement;

    if (data.total > 0) {
        container.style.display = '';
        const pct = Math.round((data.progress / data.total) * 100);
        bar.style.width = pct + '%';
        bar.textContent = pct + '%';
    } else {
        container.style.display = 'none';
    }
    msg.textContent = `[${data.step}] ${data.message}`;

    if (data.step === 'complete' || data.step === 'error') {
        addLog(`DB Update: ${data.message}`);
        setTimeout(loadDbInfo, 1000);
    }
});

// =========================================================================
// Activity log
// =========================================================================

const MAX_LOG_ENTRIES = 100;

socket.on('log_message', (data) => {
    addLog(data.message);
});

function addLog(message) {
    const log = document.getElementById('activity-log');
    if (!log) return;
    const time = new Date().toLocaleTimeString();
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.textContent = `[${time}] ${message}`;
    log.prepend(entry);
    // Trim old entries
    while (log.children.length > MAX_LOG_ENTRIES) {
        log.removeChild(log.lastChild);
    }
}

// =========================================================================
// Initialization
// =========================================================================

// =========================================================================
// Wishlist
// =========================================================================

async function loadWishlist() {
    try {
        const data = await apiGet('/api/collection/wishlist');
        const tbody = document.getElementById('wishlist-body');
        const items = data.items || [];
        document.getElementById('wishlist-count').textContent = items.filter(i => !i.found).length;
        tbody.innerHTML = '';
        for (const w of items) {
            const priceStr = w.max_price ? `$${w.max_price.toFixed(2)}` : '';
            const foundClass = w.found ? 'text-decoration-line-through text-muted' : '';
            const priorityBadge = w.priority === 'high'
                ? '<span class="badge bg-danger">High</span>'
                : w.priority === 'low'
                    ? '<span class="badge bg-secondary">Low</span>'
                    : '<span class="badge bg-info">Normal</span>';
            tbody.innerHTML += `<tr class="${foundClass}">
                <td>${w.name}</td>
                <td class="small">${w.set_code || ''}</td>
                <td>${priceStr}</td>
                <td>${priorityBadge}</td>
                <td class="small">${w.notes || ''}</td>
                <td class="text-nowrap">
                    ${!w.found ? `<button class="btn btn-outline-success btn-sm py-0 px-1" onclick="markWishlistFound(${w.id})" title="Mark found">&#10003;</button>` : ''}
                    <button class="btn btn-outline-danger btn-sm py-0 px-1" onclick="deleteWishlistItem(${w.id})" title="Remove">&times;</button>
                </td>
            </tr>`;
        }
    } catch (e) {}
}

async function addWishlistItem() {
    const name = document.getElementById('wish-name').value.trim();
    if (!name) { alert('Card name is required'); return; }
    await apiPost('/api/collection/wishlist', {
        name,
        set_code: document.getElementById('wish-set').value.trim(),
        max_price: parseFloat(document.getElementById('wish-price').value) || null,
        priority: document.getElementById('wish-priority').value,
        notes: document.getElementById('wish-notes').value.trim(),
    });
    document.getElementById('wish-name').value = '';
    document.getElementById('wish-set').value = '';
    document.getElementById('wish-price').value = '';
    document.getElementById('wish-notes').value = '';
    loadWishlist();
}

async function deleteWishlistItem(id) {
    await fetch(`/api/collection/wishlist/${id}`, { method: 'DELETE' });
    loadWishlist();
}

async function markWishlistFound(id) {
    await apiPost(`/api/collection/wishlist/${id}/found`);
    loadWishlist();
}

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
    const calTab = document.querySelector('a[href="#tab-calibration"]');
    if (calTab) {
        calTab.addEventListener('shown.bs.tab', () => {
            if (!arucoLiveActive) toggleArucoLive();
            // Refresh the saved-setups dropdown every time the user
            // navigates to the tab so a setup saved from elsewhere
            // (or a file manually dropped into the scripts dir) shows
            // up without having to reload the page.
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

// Live "marker visible" events from the calibration sweep — shows which
// markers the camera is currently seeing at each sweep step, plus a
// running count of markers that have been locked in.
socket.on('calibration_marker_visible', (data) => {
    const msg = document.getElementById('cal-progress-message');
    if (!msg) return;
    const parts = data.markers.map(m => `ID ${m.id} (${m.type})`);
    const lockedPart = (data.locked_count != null)
        ? ` · locked ${data.locked_count}`
        : '';
    msg.textContent =
        `Carriage X=${data.carriage_x}mm · visible: `
        + (parts.join(', ') || '(none)')
        + lockedPart;
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

socket.on('hardware_setup_progress', (data) => {
    const msg = document.getElementById('cal-progress-message');
    if (msg) {
        msg.textContent = `[${data.step}] ${data.message}`;
    }
});

socket.on('hardware_setup_complete', (data) => {
    const btn = document.getElementById('btn-new-hw-setup');
    if (btn) btn.disabled = false;
    const retryBtn = document.getElementById('btn-retry-hw-setup');
    if (retryBtn) retryBtn.disabled = false;
    document.getElementById('btn-start-cal').disabled = false;
    document.getElementById('btn-cancel-cal').style.display = 'none';

    const msg = document.getElementById('cal-progress-message');
    if (!data.success) {
        if (msg) msg.textContent = `Setup failed: ${data.error || 'unknown'}`;
        return;
    }

    const staging = data.staging
        ? ` · staging X=${Math.round(data.staging.x)}mm`
        : '';
    if (msg) {
        msg.textContent =
            `Setup complete: ${data.dest_bin_count} dest bins, ` +
            `${data.source_bin_count} source bins${staging}`;
    }

    // Render probe results summary
    if (data.probe_results && Object.keys(data.probe_results).length > 0) {
        let html = '<div class="mt-2 small"><strong>Probed heights:</strong><table class="table table-sm mt-1"><thead><tr><th>Bin</th><th>X (mm)</th><th>Z (mm)</th></tr></thead><tbody>';
        const locations = data.locations || {};
        const binNums = Object.keys(data.probe_results).map(Number).sort((a, b) => a - b);
        for (const bn of binNums) {
            const z = data.probe_results[bn];
            const x = locations[bn] !== undefined ? Math.round(locations[bn]) : '?';
            html += `<tr><td>${bn}</td><td>${x}</td><td>${z.toFixed(2)}</td></tr>`;
        }
        html += '</tbody></table></div>';
        const el = document.getElementById('cal-discovered-bins');
        if (el) el.innerHTML += html;
    }
});

socket.on('calibration_progress', (data) => {
    const msg = document.getElementById('cal-progress-message');
    const bar = document.getElementById('cal-progress-bar');
    const container = document.getElementById('cal-progress-container');

    msg.textContent = data.message;

    if (data.total_steps > 0) {
        container.style.display = '';
        const pct = Math.round((data.progress / data.total_steps) * 100);
        bar.style.width = pct + '%';
        bar.textContent = pct + '%';
    }

    // Update discovered bins list
    if (data.discovered && data.discovered.length > 0) {
        let html = '<table class="table table-sm"><thead><tr><th>Marker</th><th>Type</th><th>X (mm)</th></tr></thead><tbody>';
        for (const d of data.discovered) {
            const typeClass = d.type === 'source' ? 'text-info' : d.type === 'staging' ? 'text-warning' : 'text-success';
            html += `<tr><td>ID ${d.marker_id}</td><td class="${typeClass}">${d.type.toUpperCase()}</td><td>${d.bin_x}</td></tr>`;
        }
        html += '</tbody></table>';
        document.getElementById('cal-discovered-bins').innerHTML = html;
    }

    if (!data.running) {
        document.getElementById('btn-start-cal').disabled = false;
        document.getElementById('btn-cancel-cal').style.display = 'none';
        addLog(`Calibration: ${data.message}`);
    }
});

socket.on('calibration_complete', (data) => {
    const staging = data.staging ? `, staging at X=${data.staging.x}mm` : '';
    addLog(`Calibration complete: ${data.total_source} sources, ${data.total_dest} destinations${staging}`);
    // Reload bin table since bins changed
    loadBinTable();
    loadBinLocations();
    loadSourceBinsStatus();
});

// Running table of markers that have been locked in during the current
// sweep. Rebuilt from scratch on every sweep start.
let _calLockedMarkers = {};

socket.on('calibration_marker_found', (data) => {
    const samples = data.sample_count != null
        ? ` [${data.sample_count} samples]`
        : '';
    addLog(`Locked marker ${data.marker_id} (${data.type}): X=${data.bin_x}mm${samples}`);
    _calLockedMarkers[data.marker_id] = data;
    renderLockedMarkersTable();
});

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

// Rejected markers (false positives filtered out after sweep).
// Backend emits this once at the end of run_sweep_calibration with an
// array of {marker_id, type, bin_x, sample_count, reason} entries.
socket.on('calibration_rejections', (data) => {
    const el = document.getElementById('cal-rejected-markers');
    if (!el) return;
    const rejections = (data && data.rejections) || [];
    if (rejections.length === 0) {
        el.innerHTML = '';
        return;
    }
    let html =
        '<div class="small mt-3"><strong class="text-danger">' +
        'Rejected markers (' + rejections.length + '):</strong>' +
        ' <span class="text-muted">filtered as likely false positives</span></div>' +
        '<table class="table table-sm table-danger"><thead><tr>' +
        '<th>&#10007;</th><th>ID</th><th>Type</th><th>X (mm)</th>' +
        '<th>Samples</th><th>Reason</th>' +
        '</tr></thead><tbody>';
    for (const r of rejections) {
        const typeStr = (r.type || '').toUpperCase();
        const xStr = (r.bin_x != null) ? Number(r.bin_x).toFixed(1) : '?';
        const samples = r.sample_count != null ? r.sample_count : '-';
        const reason = r.reason || '';
        html += `<tr>
            <td class="text-danger">&#10007;</td>
            <td>${r.marker_id}</td>
            <td>${typeStr}</td>
            <td>${xStr}</td>
            <td>${samples}</td>
            <td class="small">${reason}</td>
        </tr>`;
        addLog(`Rejected marker ${r.marker_id} (${typeStr}) at X=${xStr}mm: ${reason}`);
    }
    html += '</tbody></table>';
    el.innerHTML = html;
});

socket.on('bin_empty_check', (data) => {
    const status = data.empty ? 'EMPTY' : 'has cards';
    addLog(`Source bin ${data.bin_number}: ${status}`);
    loadSourceBinsStatus();
});

socket.on('source_bins_status', (data) => {
    loadSourceBinsStatus();
});

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
            arrow.textContent = _reviewSortDir === 'asc' ? ' \u25B2' : ' \u25BC';
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
// Price updates
// =========================================================================

async function startPriceUpdate() {
    const resp = await apiPost('/api/collection/prices/update');
    if (resp.error) {
        alert(resp.error);
        return;
    }
    document.getElementById('btn-price-update').disabled = true;
    document.getElementById('btn-price-stop').style.display = '';
    document.getElementById('price-update-status').style.display = 'block';
    document.getElementById('price-update-status').textContent = 'Starting...';
    document.getElementById('price-progress-container').style.display = '';
}

async function stopPriceUpdate() {
    await apiPost('/api/collection/prices/stop');
}

socket.on('price_update_progress', (data) => {
    const statusEl = document.getElementById('price-update-status');
    const bar = document.getElementById('price-progress-bar');
    const container = document.getElementById('price-progress-container');

    statusEl.style.display = 'block';
    statusEl.textContent = data.message;

    if (data.total > 0) {
        container.style.display = '';
        const pct = Math.round((data.progress / data.total) * 100);
        bar.style.width = pct + '%';
        bar.textContent = pct + '%';
    }

    if (!data.running) {
        document.getElementById('btn-price-update').disabled = false;
        document.getElementById('btn-price-stop').style.display = 'none';
        // Refresh inventory to show new prices
        loadInventory(_inventoryPage);
        loadCollectionStats();
        addLog(`Price update: ${data.message}`);
    }
});

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

socket.on('continuous_sort_started', (data) => {
    _continuousActive = true;
    const btn = document.getElementById('btn-continuous');
    btn.classList.remove('btn-outline-primary');
    btn.classList.add('btn-primary');
    btn.innerHTML = '&#9632; Stop Continuous';
    document.getElementById('btn-detect').disabled = true;
    addLog(`Continuous sort started (delay=${data.delay}s)`);
});

socket.on('continuous_sort_stopped', (data) => {
    _continuousActive = false;
    const btn = document.getElementById('btn-continuous');
    btn.classList.remove('btn-primary');
    btn.classList.add('btn-outline-primary');
    btn.innerHTML = '&#9654; Continuous';
    if (currentState === 'sorting') {
        document.getElementById('btn-detect').disabled = false;
    }
    addLog(`Continuous sort stopped: ${data.reason || 'user'}`);
});

// Track undo availability from card_detected events
socket.on('card_detected', (data) => {
    if (data.undo_available !== undefined) {
        _undoAvailable = data.undo_available;
        document.getElementById('btn-undo').disabled = !_undoAvailable || currentState !== 'sorting';
    }
});

function undoLastSort() {
    if (!_undoAvailable) return;
    apiPost('/api/session/undo');
    _undoAvailable = false;
    document.getElementById('btn-undo').disabled = true;
}

socket.on('sort_undone', (data) => {
    _undoAvailable = false;
    document.getElementById('btn-undo').disabled = true;
    addLog(`UNDO: "${data.name}" returned from bin ${data.from_bin} to source`);
});

socket.on('wishlist_match', (data) => {
    addLog(`\u2B50 WISHLIST MATCH: ${data.name} -> Wishlist bin ${data.bin}`);
});

socket.on('bin_fullness_warning', (data) => {
    addLog(`\u26A0\uFE0F BIN ${data.bin} NEARLY FULL! Only ${data.remaining_mm.toFixed(1)}mm clearance.`);
});

// =========================================================================
// Wishlist Bin + Re-home Interval
// =========================================================================

function setWishlistBin() {
    const val = document.getElementById('wishlist-bin-input').value;
    const binNum = val ? parseInt(val) : null;
    apiPost('/api/session/wishlist-bin', { bin: binNum });
    addLog(binNum ? `Wishlist bin set to ${binNum}` : 'Wishlist bin disabled');
}

function setRehomeInterval() {
    const val = parseInt(document.getElementById('rehome-interval').value) || 100;
    apiPost('/api/session/rehome-interval', { interval: val });
    addLog(`Re-home interval set to every ${val} cards`);
}

// =========================================================================
// Set-based Sort Configuration
// =========================================================================

let _setConfigRows = [];

async function loadSetConfig() {
    const data = await apiGet('/api/sort/set-config');
    const container = document.getElementById('set-bin-rows');
    _setConfigRows = [];
    container.innerHTML = '';
    if (data.config && data.config.length > 0) {
        for (const entry of data.config) {
            addSetBinRow(entry.bin, entry.sets.join(', '));
        }
    } else {
        // Add 2 default empty rows
        addSetBinRow(1, '');
        addSetBinRow(2, '');
    }
}

function addSetBinRow(binNum, setsStr) {
    const container = document.getElementById('set-bin-rows');
    const idx = container.children.length;
    const bin = binNum || (idx + 1);
    const sets = setsStr || '';
    const row = document.createElement('div');
    row.className = 'd-flex gap-2 mb-1 align-items-center';
    row.innerHTML = `
        <div class="input-group input-group-sm">
            <span class="input-group-text">Bin</span>
            <input type="number" class="form-control set-bin-num" value="${bin}" min="1" max="10" style="max-width:70px;">
            <span class="input-group-text">Sets</span>
            <input type="text" class="form-control set-bin-codes" value="${sets}" placeholder="e.g. khm, neo, vow">
            <button class="btn btn-outline-danger btn-sm" onclick="this.closest('.d-flex').remove()">&times;</button>
        </div>`;
    container.appendChild(row);
}

async function saveSetConfig() {
    const rows = document.querySelectorAll('#set-bin-rows .d-flex');
    const config = [];
    for (const row of rows) {
        const bin = parseInt(row.querySelector('.set-bin-num').value);
        const setsStr = row.querySelector('.set-bin-codes').value.trim();
        if (setsStr && bin) {
            const sets = setsStr.split(',').map(s => s.trim().toLowerCase()).filter(s => s);
            config.push({ bin, sets });
        }
    }
    await apiPost('/api/sort/set-config', { config: config.length > 0 ? config : null });
    addLog(`Set sort config applied: ${config.length} bin assignment(s)`);
}

async function clearSetConfig() {
    await apiPost('/api/sort/set-config', { config: null });
    document.getElementById('set-bin-rows').innerHTML = '';
    addSetBinRow(1, '');
    addSetBinRow(2, '');
    addLog('Set sort config cleared (using default)');
}

// =========================================================================
// Theme toggle
// =========================================================================

function toggleTheme() {
    const html = document.documentElement;
    const current = html.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    const btn = document.getElementById('theme-toggle');
    btn.textContent = next === 'dark' ? '\u2606' : '\u263E';  // star / moon
    btn.title = next === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
}

function _initTheme() {
    const saved = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    const btn = document.getElementById('theme-toggle');
    if (btn) {
        btn.textContent = saved === 'dark' ? '\u2606' : '\u263E';
        btn.title = saved === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    }
}

document.addEventListener('DOMContentLoaded', () => {
    _initTheme();
    // Load initial data
    loadBinLocations();
    loadBinTable();
    drawMotionCanvas();

    // Load collection data when tab is shown
    document.querySelector('a[href="#tab-collection"]').addEventListener('shown.bs.tab', () => {
        loadCollectionStats();
        loadInventory();
        loadSessionHistory();
        loadBoxList();
        loadBoxSummary();
        loadWishlist();
        loadReviewQueue();
    });

    // Sortable column headers
    document.querySelectorAll('#tab-collection th.sortable').forEach(th => {
        th.style.cursor = 'pointer';
        th.addEventListener('click', () => sortInventory(th.dataset.sort));
    });

    // Add Card autocomplete
    _initAddCardAutocomplete();

    document.querySelector('a[href="#tab-database"]').addEventListener('shown.bs.tab', () => {
        loadDbInfo();
    });

    document.querySelector('a[href="#tab-bins"]').addEventListener('shown.bs.tab', () => {
        loadBinTable();
        loadBinConfigList();
        loadOverflowConfig();
    });

    document.querySelector('a[href="#tab-calibration"]').addEventListener('shown.bs.tab', () => {
        loadCameraOffset();
        loadSourceBinsStatus();
    });

    // Sort Session tab: start the camera feed immediately so the user
    // can see the staging area before pressing Start Session. Without
    // this, the feed only started on the `session_started` socket
    // event, so opening the tab just showed a blank camera box and
    // made the whole screen look "dead".
    document.querySelector('a[href="#tab-session"]').addEventListener('shown.bs.tab', () => {
        startCameraFeed('session-camera-feed');
        // Sync custom bin count AND fallback from hardware config (was
        // previously triggered by the separate Sort Config tab; that tab
        // has been merged into the Sort Session tab.)
        apiGet('/api/bins/config').then(data => {
            const binCount = Object.keys(data.locations || {}).filter(k => k !== '0').length;
            if (binCount > 0) {
                document.getElementById('custom-bin-count').value = binCount;
                // Also sync fallback to match bin count so SortConfig
                // validation doesn't reject fallback > bin_count.
                const fbEl = document.getElementById('custom-fallback');
                if (fbEl && parseInt(fbEl.value) > binCount) {
                    fbEl.value = binCount;
                }
                // Rebuild inputs to match the new count
                buildCustomQueryInputs();
            }
        }).catch(() => {});
    });

    document.querySelector('a[href="#tab-motion"]').addEventListener('shown.bs.tab', () => {
        drawMotionCanvas();
        // Sync sort mode display from Sort Config tab
        const label = document.getElementById('sort-mode').selectedOptions[0]?.text || 'Color';
        const simDisplay = document.getElementById('sim-mode-display');
        if (simDisplay) simDisplay.textContent = label;
    });

    // Initial state poll
    apiGet('/api/status').then(data => {
        currentState = data.state;
        updateStateBadge();
        updateSessionButtons();
        if (data.motion) {
            motionState = data.motion;
            drawMotionCanvas();
        }
    }).catch(() => {});

    // Camera status poll
    setInterval(async () => {
        try {
            const data = await apiGet('/api/camera/status');
            document.getElementById('cam-status').textContent = data.active ? 'Active' : 'Inactive';
            document.getElementById('cam-fps').textContent = data.fps || '--';
        } catch (e) {}
    }, 5000);

    updateStateBadge();
    buildCustomQueryInputs();
});
