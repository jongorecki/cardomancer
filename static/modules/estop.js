// estop.js — Emergency stop
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
