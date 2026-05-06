// state.js — Connection/session state badges + activity log
// =========================================================================
// State management
// =========================================================================

let currentState = 'disconnected';
let sessionActive = false;

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

    // Lock / unlock the sort configuration panel while a session is active.
    _lockSortConfigPanel(active);
}

// =========================================================================
// Activity log
// =========================================================================

const MAX_LOG_ENTRIES = 100;

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
