// bins.js — Bin layout/config

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
