// api.js — HTTP helpers
// =========================================================================
// API helpers
// =========================================================================
//
// CSRF defence: every non-GET request must carry
// `X-Requested-With: XMLHttpRequest`. The Flask `_csrf_origin_check`
// before_request hook in web_server.py accepts that header as proof
// the request originated from our own JS (cross-origin browsers
// can't set custom headers without a CORS preflight we don't serve).
//
// We monkey-patch `window.fetch` once at module load so legacy call
// sites that hand-roll `fetch('/api/...', { method: 'POST' })` get
// the header automatically — saving us from grepping every fetch
// call in the static/ tree.
(function _installCsrfFetchShim() {
    if (window._cmFetchShimmed) return;
    const _origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        const opts = init || {};
        const method = (opts.method || 'GET').toUpperCase();
        // Only inject on state-changing methods. Leave GET/HEAD/
        // OPTIONS alone so caches and preflight responses behave.
        if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
            const headers = new Headers(opts.headers || {});
            if (!headers.has('X-Requested-With')) {
                headers.set('X-Requested-With', 'XMLHttpRequest');
            }
            opts.headers = headers;
        }
        return _origFetch(input, opts);
    };
    window._cmFetchShimmed = true;
})();

async function apiGet(url) {
    const resp = await fetch(url);
    return resp.json();
}

async function apiPost(url, data) {
    let resp;
    try {
        resp = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
            },
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
