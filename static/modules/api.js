// api.js — HTTP helpers
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

// User-facing API error. Routes through the errors.json catalog
// (errors.js) so the user sees a friendly toast with a what-happened
// + what-to-try line instead of a raw alert. Falls back to alert()
// only if errors.js hasn't loaded yet (very early page lifetime).
function showApiError(code, message) {
    console.warn(`[api] ${code}: ${message}`);
    try {
        if (typeof addLog === 'function') {
            addLog(`Error (${code}): ${message}`);
        }
    } catch (_) {}
    if (typeof showError === 'function') {
        showError(code, message);
    } else {
        alert(`${message}`);
    }
}
