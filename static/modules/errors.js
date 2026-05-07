// errors.js — User-facing error toast/dialog driven by static/errors.json
//
// Phase 3 part of "feels like a product, not a script with a UI."
// Replaces the previous alert()-on-every-error pattern with a non-blocking
// toast that includes a human-readable title, what-happened, and a
// what-to-try next step. Unknown error codes still surface (with the
// raw message) so we can identify gaps in the catalog.
//
// Loads on page boot. Errors raised before the catalog is loaded fall
// back to the raw message — the catalog typically arrives in <100ms,
// so this is fine for human-driven actions.

let _errorCatalog = null;
let _errorCatalogPromise = null;

function _loadErrorCatalog() {
    if (_errorCatalog) return Promise.resolve(_errorCatalog);
    if (_errorCatalogPromise) return _errorCatalogPromise;
    _errorCatalogPromise = fetch('/static/errors.json')
        .then((r) => r.ok ? r.json() : null)
        .then((data) => {
            _errorCatalog = data || {};
            return _errorCatalog;
        })
        .catch(() => {
            _errorCatalog = {};
            return _errorCatalog;
        });
    return _errorCatalogPromise;
}

// Kick off the load on script parse — by the time the user does anything
// errorable, the catalog is already in memory.
_loadErrorCatalog();

function _resolveErrorEntry(code) {
    if (!_errorCatalog) return null;
    if (code && Object.prototype.hasOwnProperty.call(_errorCatalog, code)) {
        return _errorCatalog[code];
    }
    return _errorCatalog._default || null;
}

function _ensureErrorToastContainer() {
    let host = document.getElementById('error-toast-host');
    if (host) return host;
    host = document.createElement('div');
    host.id = 'error-toast-host';
    host.style.position = 'fixed';
    host.style.top = '70px';
    host.style.right = '20px';
    host.style.zIndex = 'var(--z-toast, 1080)';
    host.style.maxWidth = '420px';
    host.style.display = 'flex';
    host.style.flexDirection = 'column';
    host.style.gap = '8px';
    document.body.appendChild(host);
    return host;
}

const _SEVERITY_BG = {
    info: 'alert-info',
    warning: 'alert-warning',
    error: 'alert-danger',
};

function showError(code, fallbackMessage) {
    // Async path: if the catalog is in flight, we still want to show
    // SOMETHING immediately so the user gets feedback. Defer entry
    // resolution and re-render once it's loaded.
    const host = _ensureErrorToastContainer();
    const entry = _resolveErrorEntry(code);

    const toast = document.createElement('div');
    const severity = (entry && entry.severity) || 'error';
    const bgClass = _SEVERITY_BG[severity] || 'alert-danger';
    toast.className = `alert ${bgClass} alert-dismissible fade show`;
    toast.style.boxShadow = 'var(--shadow-md, 0 2px 8px rgba(0,0,0,0.3))';
    toast.style.marginBottom = '0';

    const title = entry ? entry.title : 'Something went wrong';
    const what = entry ? entry.what_happened : '';
    const action = entry ? entry.what_to_try : '';
    const technical = fallbackMessage || code || '';
    const technicalSection = technical
        ? `<details class="mt-2 small">
              <summary class="text-muted">Technical details</summary>
              <code class="d-block mt-1" style="word-break:break-all">
                  ${escapeHtml(String(technical))}
              </code>
              ${code ? `<div class="text-muted small mt-1">code: <code>${escapeHtml(code)}</code></div>` : ''}
           </details>`
        : '';

    toast.innerHTML = `
        <strong>${escapeHtml(title)}</strong>
        ${what ? `<div class="mt-1">${escapeHtml(what)}</div>` : ''}
        ${action ? `<div class="mt-1"><em>${escapeHtml(action)}</em></div>` : ''}
        ${technicalSection}
        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
    `;
    host.appendChild(toast);

    // Auto-dismiss info-level toasts after 6s. Warnings + errors stay
    // until the user dismisses them — the user needs to actually read
    // the action they should take.
    if (severity === 'info') {
        setTimeout(() => {
            try { toast.classList.remove('show'); } catch (_) {}
            setTimeout(() => { try { toast.remove(); } catch (_) {} }, 200);
        }, 6000);
    }

    // If the catalog wasn't loaded yet when this fired, the toast was
    // rendered with default text. Re-render once it arrives so the user
    // sees the friendly copy on the next refresh / dismiss-and-retry
    // cycle. (Re-rendering THIS toast in place is more invasive than
    // it's worth — the catalog typically loads before any error.)
    if (!_errorCatalog) {
        _loadErrorCatalog();
    }
}
