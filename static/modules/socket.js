// socket.js — SocketIO connection + all socket handlers
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
// State management handlers
// =========================================================================

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

// Power-loss / unclean-shutdown recovery — see session.js for the
// banner UI. Phase 2 ships detect + discard; Phase 4 will land Resume.
socket.on('stale_session_detected', (data) => {
    if (typeof showStaleSessionBanner === 'function') {
        showStaleSessionBanner(data);
    }
});

socket.on('stale_session_discarded', (data) => {
    if (typeof onStaleSessionDiscarded === 'function') {
        onStaleSessionDiscarded(data);
    }
});

// Bin chain exhausted — session paused per autonomy ladder rule
// "machine never stops unless it has to."
socket.on('bin_full_prompt', (data) => {
    if (typeof showBinFullBanner === 'function') {
        showBinFullBanner(data);
    }
});

// When the user clicks Resume after emptying a bin, the worker emits
// session_resumed (via _cmd_resume → state setter sorter_state event).
// Use that to clear the bin-full banner.
socket.on('sorter_state', (data) => {
    if (data.state === 'sorting' && typeof dismissBinFullBanner === 'function') {
        dismissBinFullBanner();
    }
});

// =========================================================================
// Card detection events
// =========================================================================

socket.on('card_detected', (data) => {
    const panel = document.getElementById('last-card-info');
    if (data.recognized) {
        // Border badge: colored pill per Scryfall border_color. Shown
        // on every recognized card (not just borderless).
        const borderClass = {
            'black':      'bg-dark text-light',
            'white':      'bg-light text-dark border',
            'borderless': 'bg-info text-dark',
            'silver':     'bg-secondary text-light',
            'gold':       'bg-warning text-dark',
        }[(data.border || '').toLowerCase()] || 'bg-secondary text-light';
        const borderLabel = data.border
            ? data.border.charAt(0).toUpperCase() + data.border.slice(1)
            : 'Unknown';
        const borderBadge = ` <span class="badge ${borderClass}">${borderLabel}</span>`;
        // Foil badge: gold pill when the scan was detected as a
        // physical foil printing (from foil_detect).
        const foilBadge = data.is_foil
            ? ' <span class="badge bg-warning text-dark">★ Foil</span>' : '';
        // Frame era badge: Scryfall "frame" field ("1993", "1997",
        // "2003", "2015", "future"). Shown for every recognized card
        // so the printing's frame era is visible at a glance.
        const frameLabel = data.frame
            ? (data.frame === 'future' ? 'Future' : `${data.frame} frame`)
            : '';
        const frameBadge = frameLabel
            ? ` <span class="badge bg-secondary">${frameLabel}</span>` : '';
        // Frame-effects badges: one per modifier (showcase, etched,
        // extendedart, inverted, etc.). Each becomes its own small pill.
        const frameEffects = Array.isArray(data.frame_effects) ? data.frame_effects : [];
        const frameEffectsBadges = frameEffects
            .map(fx => ` <span class="badge bg-info text-dark">${fx}</span>`)
            .join('');
        const layoutBadge = data.layout && data.layout !== 'normal'
            ? ` <span class="badge bg-warning text-dark">${data.layout}</span>` : '';
        const cn = data.collector_number ? ` #${data.collector_number}` : '';
        panel.innerHTML = `
            <h5>${data.name}${borderBadge}${frameBadge}${frameEffectsBadges}${foilBadge}${layoutBadge}</h5>
            <p class="mb-1">Set: <strong>${data.set}${cn}</strong> | Colors: <strong>${(data.colors || []).join('')}</strong></p>
            <p class="mb-1">Type: ${(data.types || []).join(' ')} | CMC: ${data.cmc}</p>
            <p class="mb-1">Price: ${data.price} | Rarity: ${data.rarity}</p>
            <p class="mb-0">Bin: <strong>${data.bin}</strong> | Method: <em>${data.method}</em></p>
        `;
        const foilTag = data.is_foil ? ' [FOIL]' : '';
        addLog(`Detected: ${data.name}${cn}${foilTag} -> Bin ${data.bin} (${data.method})`);
    } else {
        panel.innerHTML = `<p class="text-danger">Unrecognized card -> Bin ${data.bin || 10}</p>`;
        addLog(`Unrecognized card -> Bin ${data.bin || 10}`);
    }
});

// Hook into card_detected to draw overlay
socket.on('card_detected', (data) => {
    drawCameraOverlay(data);
});

socket.on('card_detected', (data) => {
    const panel = document.getElementById('card-overlay-panel');
    if (!panel) return;
    if (!data || !data.recognized) {
        panel.innerHTML = '<p class="text-muted small mb-0">Unrecognized card.</p>';
        return;
    }
    const reqId = ++_cardOverlayReqId;
    panel.innerHTML = '<p class="text-muted small mb-0">Loading card info&hellip;</p>';
    // Prefer the Scryfall ID so the preview renders the exact printing
    // that was matched, not some other printing of a card sharing the
    // same name and set (e.g. basic-land variants with different
    // collector numbers). Fall back to name+set for safety.
    const params = new URLSearchParams();
    if (data.id) params.set('id', data.id);
    if (data.name) params.set('name', data.name);
    if (data.set) params.set('set', data.set);
    fetch('/api/card/overlay-info?' + params.toString())
        .then((r) => (r.ok ? r.json() : null))
        .then((info) => {
            // Drop stale responses if a newer card_detected fired mid-flight.
            if (reqId !== _cardOverlayReqId) return;
            renderCardOverlay(info, data.price);
        })
        .catch(() => {
            if (reqId !== _cardOverlayReqId) return;
            renderCardOverlay(null, data.price);
        });
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
    loadDashboardBinRouting();
});

socket.on('session_ended', () => {
    loadDashboardBinRouting();
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
// Motion handlers
// =========================================================================

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

// =========================================================================
// Bin handlers
// =========================================================================

socket.on('bins_configured', (data) => {
    loadBinTable();
    drawBinLayoutCanvas();
    // Sync bin count to the sort config panel
    if (data && data.count) {
        const scBinCount = document.getElementById('sc-bin-count');
        if (scBinCount) scBinCount.value = data.count;
    }
});

// Overflow / fullness SocketIO events
socket.on('bin_full', (data) => {
    const reason = data.reason === 'probe' ? ' (detected by probe)' : '';
    addLog(`⚠️ BIN ${data.bin} IS FULL (${data.count}/${data.limit} cards)${reason}`);
    loadBinTable();
});

socket.on('bin_emptied', (data) => {
    addLog(`✅ Bin ${data.bin} marked empty (was ${data.old_count} cards)`);
    loadBinTable();
});

socket.on('bins_chain_full', (data) => {
    addLog(`🛑 ALL BINS FULL for "${data.card_name}" (logical bin ${data.logical_bin}, chain [${data.chain.join(', ')}]). SORTING PAUSED — empty bins and resume.`);
    loadBinTable();
});

socket.on('overflow_map_updated', (data) => {
    loadBinTable();
});

// =========================================================================
// Simulation handlers
// =========================================================================

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
// Database handlers
// =========================================================================

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
// Activity log handlers
// =========================================================================

socket.on('log_message', (data) => {
    addLog(data.message);
});

// =========================================================================
// Calibration handlers
// =========================================================================

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

socket.on('calibration_marker_found', (data) => {
    const samples = data.sample_count != null
        ? ` [${data.sample_count} samples]`
        : '';
    addLog(`Locked marker ${data.marker_id} (${data.type}): X=${data.bin_x}mm${samples}`);
    _calLockedMarkers[data.marker_id] = data;
    renderLockedMarkersTable();
});

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

// =========================================================================
// Price update handlers
// =========================================================================

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
// Continuous sort handlers
// =========================================================================

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

socket.on('sort_undone', (data) => {
    _undoAvailable = false;
    document.getElementById('btn-undo').disabled = true;
    addLog(`UNDO: "${data.name}" returned from bin ${data.from_bin} to source`);
});

socket.on('wishlist_match', (data) => {
    // Two shapes are emitted on this channel:
    //   legacy collection_db match: {name, bin, wishlist_item}
    //   priority-bin match (4.21):  {oracle_id, name, image_uri, set, priority_bin}
    const binNum = data.priority_bin !== undefined ? data.priority_bin : data.bin;
    addLog(`⭐ WISHLIST MATCH: ${data.name} -> bin ${binNum}`);
    // Render a non-modal toast for priority-bin matches.  Session keeps
    // scanning; the toast stays until the user dismisses it.
    if (data.priority_bin !== undefined) {
        showPriorityToast(data);
    }
});

socket.on('bin_fullness_warning', (data) => {
    addLog(`⚠️ BIN ${data.bin} NEARLY FULL! Only ${data.remaining_mm.toFixed(1)}mm clearance.`);
});

// =========================================================================
// Enrichment handlers
// =========================================================================

socket.on('enrichment_refresh_started', (data) => {
    if (data && data.source) _enrichShowProgress(data.source, {started: true, message: 'Starting…'});
});

socket.on('enrichment_refresh_progress', (data) => {
    if (!data || !data.source) return;
    _enrichShowProgress(data.source, {
        progress: data.progress,
        total: data.total,
        message: data.message || data.step,
    });
});

socket.on('enrichment_refresh_complete', (data) => {
    if (data && data.source) _enrichFinishProgress(data.source, data);
});

// =========================================================================
// Drop tuner handlers
// =========================================================================

socket.on('drop_tuner_progress', (data) => {
    _dropTunerInFlight = false;
    _dropTunerSetButtonsDisabled(false);
    const phase = data.phase || 'idle';
    _dropTunerShowPhase(phase);
    const status = document.getElementById('drop-tuner-status');
    if (status) {
        let text = data.message || '';
        if (data.bin_number !== undefined && data.bin_number !== null) {
            text = `Bin ${data.bin_number} @ X=${(data.target_x || 0).toFixed(1)}mm — ${text}`;
        }
        status.textContent = text;
    }
    const czEl = document.getElementById('drop-tuner-current-z');
    if (czEl) {
        czEl.textContent = (data.current_z !== null && data.current_z !== undefined)
            ? Number(data.current_z).toFixed(1)
            : '—';
    }
    const offEl = document.getElementById('drop-tuner-pending-offset');
    if (offEl) {
        offEl.textContent = (data.pending_offset !== undefined && data.pending_offset !== null)
            ? Number(data.pending_offset).toFixed(1)
            : '—';
    }
});

socket.on('drop_tuner_complete', (data) => {
    _dropTunerInFlight = false;
    _dropTunerSetButtonsDisabled(false);
    _dropTunerShowPhase('idle');
    const startBtn = document.getElementById('btn-drop-tuner-start');
    if (startBtn) startBtn.disabled = false;
    if (data && data.success) {
        addLog(`Drop tuner: saved Z_DROP_OFFSET = ${Number(data.new_offset).toFixed(2)}mm`);
        const offEl = document.getElementById('drop-tuner-current-offset');
        if (offEl) offEl.textContent = Number(data.new_offset).toFixed(1);
    } else if (data && data.cancelled) {
        addLog('Drop tuner: cancelled (offset unchanged)');
    } else if (data && data.error) {
        addLog(`Drop tuner: failed (${data.error})`);
    }
});

socket.on('drop_tuner_error', (data) => {
    _dropTunerInFlight = false;
    _dropTunerSetButtonsDisabled(false);
    if (!data || !data.error) return;
    if (data.error === 'at_limit') {
        // Hit a Z bound — flash the status line so the user knows the
        // step button click was acknowledged but ignored, instead of
        // wondering why the carriage didn't move.
        const limit = data.limit === 'lower' ? 'lower (bin floor)' : 'upper';
        const envelope = (data.envelope_low !== undefined && data.envelope_high !== undefined)
            ? ` [${Number(data.envelope_low).toFixed(1)}, ${Number(data.envelope_high).toFixed(1)}]`
            : '';
        const status = document.getElementById('drop-tuner-status');
        if (status) {
            status.textContent = `At ${limit} Z limit${envelope} — try the other direction.`;
            status.classList.add('text-warning');
            setTimeout(() => status.classList.remove('text-warning'), 2500);
        }
        addLog(`Drop tuner: at ${limit} Z limit (Z=${Number(data.current_z).toFixed(1)})`);
        return;
    }
    addLog(`Drop tuner error: ${data.error}${data.message ? ' — ' + data.message : ''}`);
});
