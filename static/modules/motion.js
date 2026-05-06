// motion.js — Motion tracking, canvas, and animation
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
        ctx.fillText(`→ X: ${Math.round(wp.x)}  Z: ${Math.round(wp.z)}`, margin + 220, 15);
    } else if (isMoving) {
        ctx.fillStyle = '#ff0';
        ctx.fillText(`→ X: ${motionState.dest_x}  Z: ${motionState.dest_z}`, margin + 220, 15);
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
