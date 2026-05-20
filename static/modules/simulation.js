// simulation.js — Motion simulation preview
// =========================================================================
// Simulation
// =========================================================================

let _simPollInterval = null;

function startSimulation() {
    const count = parseInt(document.getElementById('sim-card-count').value) || 10;
    const config_lines = typeof serializeSortConfig === 'function'
        ? serializeSortConfig()
        : '';
    const payload = { card_count: count, config_lines };

    apiPost('/api/sim/test-run', payload);
    renderSkeleton('sim-results', { rows: 4 });
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
        renderEmptyState(panel, {
            sigil: 'spiral',
            title: 'No results yet.',
            body: 'Run a simulation to see per-card outcomes.',
            variant: 'compact',
        });
        return;
    }
    let html = '<table class="table table-sm"><thead><tr><th>Card</th><th>Set</th><th>Bin</th></tr></thead><tbody>';
    for (const r of results) {
        html += `<tr><td>${r.card_name}</td><td>${r.set}</td><td>${r.bin}</td></tr>`;
    }
    html += '</tbody></table>';
    panel.innerHTML = html;
}
