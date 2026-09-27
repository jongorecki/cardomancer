// cameraSettings.js — Setup tab "Cameras" panel
// ---------------------------------------------------------------------------
// One card per camera role (down = carriage camera, up = camera under the
// held card). Reads GET /api/cameras, saves through POST /api/cameras/<role>
// (persisted server-side in camera_config.json). Manual controls apply live.
// Status refreshes only while the panel is on screen; previews are torn down
// when it isn't, so hidden MJPEG streams don't keep the cameras busy.
// ---------------------------------------------------------------------------

const CAMERA_ROLE_LABELS = {
    down: 'Carriage camera (down)',
    up: 'Card camera (up)',
};

const CAMERA_MODES = {
    '1080p30': { width: 1920, height: 1080, fps: 30 },
    '1080p60': { width: 1920, height: 1080, fps: 60 },
    '4k30': { width: 3840, height: 2160, fps: 30 },
};

// Driver units (DirectShow): exposure is log2 seconds (-6 = 1/64 s),
// auto_exposure is 0.75 auto / 0.25 manual. Ranges are typical UVC
// limits; drivers clamp or ignore values they don't support.
const CAMERA_CONTROLS = [
    { key: 'exposure', auto: 'auto_exposure', autoOn: 0.75, autoOff: 0.25,
      label: 'Exposure', min: -13, max: -1, step: 1, def: -6,
      fmt: v => '1/' + Math.round(1 / Math.pow(2, v)) + ' s' },
    { key: 'focus', auto: 'autofocus', autoOn: 1, autoOff: 0,
      label: 'Focus', min: 0, max: 255, step: 1, def: 128, fmt: v => String(v) },
    { key: 'wb_temperature', auto: 'auto_wb', autoOn: 1, autoOff: 0,
      label: 'White balance', min: 2800, max: 6500, step: 100, def: 4600,
      fmt: v => v + ' K' },
];

let _camPanelState = { cameras: {}, devices: [], idRole: null };
const _camPreviewOn = {};
const _camControlTimers = {};

function _camPanelVisible() {
    const el = document.getElementById('camera-settings-panel');
    return !!(el && el.offsetParent !== null && !document.hidden);
}

function _camModeKey(s) {
    const [w, h] = s.resolution || [];
    for (const [key, m] of Object.entries(CAMERA_MODES)) {
        if (m.width === w && m.height === h && m.fps === s.target_fps) return key;
    }
    return '';
}

function _camRotation(role) {
    const el = document.getElementById(`cam-${role}-rotate`);
    return el ? parseInt(el.value, 10) : 0;
}

async function loadCameraSettings() {
    try {
        const data = await apiGet('/api/cameras');
        _camPanelState = {
            cameras: data.cameras || {},
            devices: data.devices || [],
            idRole: data.id_role,
        };
        renderCameraPanel();
    } catch (err) {
        const panel = document.getElementById('camera-settings-panel');
        if (panel) panel.textContent = 'Could not load camera settings: ' + err;
    }
}

function _cameraCardHtml(role, s) {
    const e = escapeHtml;
    const isId = role === _camPanelState.idRole;
    const devices = _camPanelState.devices;
    const deviceField = devices.length
        ? `<select class="form-select form-select-sm" id="cam-${e(role)}-device">
               ${devices.map((name, i) => `<option value="${i}"
                   ${(s.device_name ? s.device_name === name : s.device_index === i) ? 'selected' : ''}>
                   ${i}: ${e(name)}</option>`).join('')}
           </select>`
        : `<input type="number" min="0" max="9" class="form-control form-control-sm"
               id="cam-${e(role)}-device" value="${Number(s.device_index) || 0}"
               aria-describedby="cam-${e(role)}-device-help">
           <div class="form-text" id="cam-${e(role)}-device-help">
               Device names need <code>pip install pygrabber</code>; using index.</div>`;
    const modeKey = _camModeKey(s);
    const controls = s.controls || {};
    const controlRows = CAMERA_CONTROLS.map(c => {
        const isAuto = controls[c.auto] === undefined || controls[c.auto] === c.autoOn;
        const val = controls[c.key] !== undefined ? controls[c.key] : c.def;
        return `
        <div class="row g-2 align-items-center mb-1">
            <div class="col-4">
                <div class="form-check form-switch mb-0">
                    <input class="form-check-input" type="checkbox" id="cam-${e(role)}-${c.auto}"
                        ${isAuto ? 'checked' : ''}
                        onchange="onCameraControlChange('${e(role)}')">
                    <label class="form-check-label small" for="cam-${e(role)}-${c.auto}">
                        Auto ${e(c.label.toLowerCase())}</label>
                </div>
            </div>
            <div class="col-5">
                <input type="range" class="form-range" id="cam-${e(role)}-${c.key}"
                    min="${c.min}" max="${c.max}" step="${c.step}" value="${Number(val)}"
                    aria-label="${e(c.label)}" ${isAuto ? 'disabled' : ''}
                    oninput="onCameraControlChange('${e(role)}')">
            </div>
            <div class="col-3 small text-end" id="cam-${e(role)}-${c.key}-val">
                ${isAuto ? 'auto' : e(c.fmt(Number(val)))}</div>
        </div>`;
    }).join('');

    return `
    <div class="card mb-2" data-camera-role="${e(role)}">
        <div class="card-header d-flex justify-content-between align-items-center">
            <span>${e(CAMERA_ROLE_LABELS[role] || role)}
                ${isId ? '<span class="badge bg-primary ms-1" title="Card identification reads from this camera">ID camera</span>' : ''}</span>
            <span class="badge bg-secondary" id="cam-${e(role)}-health" role="status">…</span>
        </div>
        <div class="card-body">
            <div class="row g-2 mb-2">
                <div class="col-sm-6">
                    <div class="form-check form-switch">
                        <input class="form-check-input" type="checkbox" id="cam-${e(role)}-enabled"
                            ${s.enabled ? 'checked' : ''}>
                        <label class="form-check-label" for="cam-${e(role)}-enabled">Enabled</label>
                    </div>
                </div>
                <div class="col-sm-6">
                    <label class="form-label small mb-0" for="cam-${e(role)}-device">Device</label>
                    ${deviceField}
                </div>
                <div class="col-4">
                    <label class="form-label small mb-0" for="cam-${e(role)}-rotate">Rotation</label>
                    <select class="form-select form-select-sm" id="cam-${e(role)}-rotate">
                        ${[0, 90, 180, 270].map(r => `<option value="${r}">${r}°</option>`).join('')}
                    </select>
                </div>
                <div class="col-4">
                    <label class="form-label small mb-0" for="cam-${e(role)}-flip">Mirror</label>
                    <select class="form-select form-select-sm" id="cam-${e(role)}-flip">
                        <option value="none">None</option>
                        <option value="h">Horizontal</option>
                        <option value="v">Vertical</option>
                        <option value="hv">Both</option>
                    </select>
                </div>
                <div class="col-4">
                    <label class="form-label small mb-0" for="cam-${e(role)}-mode">Mode</label>
                    <select class="form-select form-select-sm" id="cam-${e(role)}-mode">
                        ${modeKey ? '' : '<option value="" selected>Custom</option>'}
                        <option value="1080p30" ${modeKey === '1080p30' ? 'selected' : ''}>1080p 30 fps</option>
                        <option value="1080p60" ${modeKey === '1080p60' ? 'selected' : ''}>1080p 60 fps</option>
                        <option value="4k30" ${modeKey === '4k30' ? 'selected' : ''}>4K 30 fps</option>
                    </select>
                </div>
            </div>
            <div class="mb-2">${controlRows}</div>
            <div class="d-flex flex-wrap gap-1 mb-2">
                <button class="btn btn-primary btn-sm" onclick="saveCameraSettings('${e(role)}')">Save settings</button>
                <button class="btn btn-success btn-sm" onclick="startCameraRole('${e(role)}')">Start</button>
                <button class="btn btn-secondary btn-sm" onclick="stopCameraRole('${e(role)}')">Stop</button>
                <button class="btn btn-outline-info btn-sm" id="cam-${e(role)}-preview-btn"
                    aria-pressed="false" onclick="toggleCameraPreview('${e(role)}')">Preview</button>
            </div>
            <p class="small text-danger mb-1" id="cam-${e(role)}-error" role="alert"></p>
            <img id="cam-${e(role)}-preview" alt="${e(CAMERA_ROLE_LABELS[role] || role)} preview"
                class="img-fluid" style="max-height:260px; background:#111; display:none;">
        </div>
    </div>`;
}

function renderCameraPanel() {
    const panel = document.getElementById('camera-settings-panel');
    if (!panel) return;
    const roles = Object.keys(_camPanelState.cameras);
    panel.innerHTML = roles.map(role =>
        `<div class="col-lg-6">${_cameraCardHtml(role, _camPanelState.cameras[role])}</div>`
    ).join('');
    for (const role of roles) {
        const s = _camPanelState.cameras[role];
        const rot = document.getElementById(`cam-${role}-rotate`);
        if (rot) rot.value = String(s.rotate_degrees ?? 0);
        const flip = document.getElementById(`cam-${role}-flip`);
        if (flip) flip.value = s.flip_name || 'none';
        _updateCameraHealth(role, s);
        if (_camPreviewOn[role]) _setPreview(role, true);
    }
}

function _updateCameraHealth(role, s) {
    const badge = document.getElementById(`cam-${role}-health`);
    if (!badge) return;
    const cls = { ok: 'bg-success', stalled: 'bg-warning text-dark',
                  dead: 'bg-danger' }[s.health] || 'bg-secondary';
    badge.className = 'badge ' + cls;
    if (!s.enabled) {
        badge.textContent = 'disabled';
    } else if (s.active) {
        badge.textContent = `${s.health} · ${Number(s.fps || 0).toFixed(0)} fps`;
    } else {
        badge.textContent = 'stopped';
    }
    const errEl = document.getElementById(`cam-${role}-error`);
    if (errEl && s.enabled) errEl.textContent = s.last_error || '';
}

function _collectCameraSettings(role) {
    const val = id => document.getElementById(`cam-${role}-${id}`);
    const out = {
        enabled: val('enabled').checked,
        rotate: _camRotation(role),
        flip: val('flip').value,
        controls: _collectCameraControls(role),
    };
    const dev = val('device');
    const idx = parseInt(dev.value, 10);
    if (_camPanelState.devices.length) {
        out.device_index = idx;
        out.device_name = _camPanelState.devices[idx];
    } else {
        out.device_index = idx;
    }
    const mode = CAMERA_MODES[val('mode').value];
    if (mode) Object.assign(out, mode);
    return out;
}

function _collectCameraControls(role) {
    const controls = {};
    for (const c of CAMERA_CONTROLS) {
        const auto = document.getElementById(`cam-${role}-${c.auto}`).checked;
        controls[c.auto] = auto ? c.autoOn : c.autoOff;
        if (!auto) {
            controls[c.key] = Number(document.getElementById(`cam-${role}-${c.key}`).value);
        }
    }
    return controls;
}

async function _postCameraSettings(role, body) {
    const errEl = document.getElementById(`cam-${role}-error`);
    const res = await apiPost(`/api/cameras/${encodeURIComponent(role)}`, body);
    if (!res.ok) {
        if (errEl) errEl.textContent = res.message || res.error || 'Save failed';
        return null;
    }
    if (errEl) errEl.textContent = '';
    return res;
}

async function saveCameraSettings(role) {
    const res = await _postCameraSettings(role, _collectCameraSettings(role));
    if (res) {
        addLog(`Camera '${role}' settings saved`);
        await loadCameraSettings();
    }
}

// Controls apply live (debounced) so the preview shows the effect while
// the slider moves; they're persisted in the same call.
function onCameraControlChange(role) {
    for (const c of CAMERA_CONTROLS) {
        const auto = document.getElementById(`cam-${role}-${c.auto}`).checked;
        const slider = document.getElementById(`cam-${role}-${c.key}`);
        slider.disabled = auto;
        document.getElementById(`cam-${role}-${c.key}-val`).textContent =
            auto ? 'auto' : c.fmt(Number(slider.value));
    }
    clearTimeout(_camControlTimers[role]);
    _camControlTimers[role] = setTimeout(() => {
        _postCameraSettings(role, { controls: _collectCameraControls(role) });
    }, 250);
}

async function startCameraRole(role) {
    const res = await apiPost(`/api/camera/start?cam=${encodeURIComponent(role)}`);
    const errEl = document.getElementById(`cam-${role}-error`);
    if (errEl) errEl.textContent = res.started ? '' : (res.error || 'Camera did not start');
    refreshCameraStatus();
}

async function stopCameraRole(role) {
    _setPreview(role, false);
    await apiPost(`/api/camera/stop?cam=${encodeURIComponent(role)}`);
    refreshCameraStatus();
}

function _setPreview(role, on) {
    _camPreviewOn[role] = on;
    const img = document.getElementById(`cam-${role}-preview`);
    const btn = document.getElementById(`cam-${role}-preview-btn`);
    if (btn) btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (!img) return;
    if (on) {
        if (!img.src.includes('/api/camera/feed')) {
            img.src = `/api/camera/feed?cam=${encodeURIComponent(role)}&t=${Date.now()}`;
        }
        img.style.display = '';
    } else {
        img.removeAttribute('src');
        img.style.display = 'none';
    }
}

function toggleCameraPreview(role) {
    _setPreview(role, !_camPreviewOn[role]);
}

async function refreshCameraStatus() {
    if (!_camPanelVisible()) {
        // Tear down streams while the panel is off screen, but remember
        // which previews were on so they come back.
        for (const role of Object.keys(_camPreviewOn)) {
            const img = document.getElementById(`cam-${role}-preview`);
            if (img && img.getAttribute('src')) img.removeAttribute('src');
        }
        return;
    }
    try {
        const data = await apiGet('/api/cameras');
        _camPanelState.devices = data.devices || _camPanelState.devices;
        for (const [role, s] of Object.entries(data.cameras || {})) {
            _camPanelState.cameras[role] = s;
            _updateCameraHealth(role, s);
            if (_camPreviewOn[role]) _setPreview(role, true);
        }
    } catch (_) {}
}

document.addEventListener('DOMContentLoaded', () => {
    if (!document.getElementById('camera-settings-panel')) return;
    loadCameraSettings();
    setInterval(refreshCameraStatus, 2000);
    document.addEventListener('visibilitychange', refreshCameraStatus);
});

// ---------------------------------------------------------------------------
// Sort cycle + ID camera (docs/design/up_camera.md#reversibility).
// Both are locked while a session is running; the server refuses changes.
// ---------------------------------------------------------------------------

async function loadSortCycle() {
    const sel = document.getElementById('sort-cycle-select');
    if (!sel) return;
    try {
        const data = await apiGet('/api/machine/sort-cycle');
        sel.value = data.sort_cycle;
        document.getElementById('id-camera-select').value = data.id_role;
        _updateSortCycleHint(data.sort_cycle, data.id_role);
    } catch (_) {}
}

function _updateSortCycleHint(cycle, idRole) {
    const hint = document.getElementById('sort-cycle-hint');
    if (!hint) return;
    if (cycle === 'upcam' && idRole !== 'up') {
        hint.className = 'col-sm-4 small text-warning';
        hint.textContent = 'Up-camera cycle needs the card camera (up) as ID camera.';
    } else {
        hint.className = 'col-sm-4 small text-muted';
        hint.textContent = 'Takes effect when the next session starts.';
    }
}

async function setSortCycle(cycle) {
    const res = await apiPost('/api/machine/sort-cycle', { sort_cycle: cycle });
    if (res.ok) addLog(`Sort cycle set to '${cycle}'`);
    await loadSortCycle();
}

async function setIdCamera(role) {
    const res = await apiPost('/api/camera/id-role', { role });
    if (res.ok) addLog(`Card ID camera set to '${role}'`);
    await loadSortCycle();
    await loadCameraSettings();
}

document.addEventListener('DOMContentLoaded', loadSortCycle);
