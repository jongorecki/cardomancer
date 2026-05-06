// enrichment.js — Enrichment sources + price update + dashboard bin routing
// =========================================================================
// Price updates
// =========================================================================

async function startPriceUpdate() {
    const resp = await apiPost('/api/collection/prices/update');
    if (resp.error) {
        alert(resp.error);
        return;
    }
    document.getElementById('btn-price-update').disabled = true;
    document.getElementById('btn-price-stop').style.display = '';
    document.getElementById('price-update-status').style.display = 'block';
    document.getElementById('price-update-status').textContent = 'Starting...';
    document.getElementById('price-progress-container').style.display = '';
}

async function stopPriceUpdate() {
    await apiPost('/api/collection/prices/stop');
}

// -------- Phase 0A additions (enrichment sources + collection sub-nav) --------

async function loadEnrichmentSources() {
    const container = document.getElementById('enrichment-sources');
    if (!container) return;
    try {
        const data = await apiGet('/api/enrichment/sources');
        const sources = (data && data.sources) || [];
        if (!sources.length) {
            container.innerHTML = '<em class="text-muted">No sources registered.</em>';
            return;
        }
        const rows = sources.map(s => {
            const last = s.last_result && s.last_result.success
                ? '<span class="text-success">ok</span>'
                : (s.last_result
                    ? '<span class="text-danger">err</span>'
                    : '<span class="text-muted">never</span>');
            const running = s.running ? ' <span class="badge bg-warning">running</span>' : '';
            const progressHidden = s.running ? '' : 'style="display:none;"';
            return `<div class="border-bottom py-1" data-enrich-source="${s.name}">
                <div class="d-flex justify-content-between align-items-center">
                    <span><strong>${s.name}</strong> <code class="small">${s.cron || ''}</code><span data-enrich-running>${running}</span></span>
                    <span>
                        <span data-enrich-status>${last}</span>
                        <button class="btn btn-sm btn-outline-primary ms-2"
                                data-enrich-refresh-btn
                                ${s.running ? 'disabled' : ''}
                                onclick="triggerEnrichmentRefresh('${s.name}')">Refresh</button>
                    </span>
                </div>
                <div data-enrich-progress ${progressHidden}>
                    <div class="progress mt-1" style="height:6px;">
                        <div class="progress-bar progress-bar-striped progress-bar-animated"
                             role="progressbar"
                             data-enrich-bar
                             style="width:0%"></div>
                    </div>
                    <div class="small text-muted mt-1" data-enrich-msg>Starting…</div>
                </div>
            </div>`;
        }).join('');
        container.innerHTML = rows;
    } catch (e) {
        container.innerHTML = `<span class="text-danger small">Failed to load: ${e.message || e}</span>`;
    }
}

async function loadDashboardBinRouting() {
    const container = document.getElementById('dashboard-bin-routing');
    if (!container) return;
    try {
        const [cfg, fullness] = await Promise.all([
            apiGet('/api/sort/current'),
            apiGet('/api/bins/fullness'),
        ]);
        if (!cfg || !cfg.active) {
            container.innerHTML = '<em class="text-muted small">No active sort config. Start a session from the Sort Session tab.</em>';
            return;
        }
        const counts = fullness.bin_card_counts || {};
        const fullSet = new Set((fullness.bins_full || []).map(String));
        const limit = fullness.bin_card_limit || 150;
        const queries = cfg.bin_queries || {};
        const overrides = new Set((cfg.overrides || []).map(String));

        const rows = [];
        for (let i = 1; i <= cfg.bin_count; i++) {
            const key = String(i);
            const q = queries[key] || '';
            const count = counts[key] || 0;
            const isFull = fullSet.has(key);
            const isFallback = i === cfg.fallback_bin;
            const isOverride = overrides.has(key);
            const badges = [];
            if (isFallback) badges.push('<span class="badge bg-secondary">fallback</span>');
            if (isOverride) badges.push('<span class="badge bg-info">override</span>');
            if (isFull) badges.push('<span class="badge bg-danger">full</span>');
            const queryCell = q
                ? `<code class="small">${escapeHtml(q)}</code>`
                : (isFallback
                    ? '<em class="text-muted small">(unmatched cards)</em>'
                    : '<em class="text-muted small">(no query)</em>');
            rows.push(`
                <tr${isFull ? ' class="table-danger"' : ''}>
                    <td class="text-muted">${i}</td>
                    <td>${queryCell} ${badges.join(' ')}</td>
                    <td class="text-end"><strong>${count}</strong> <span class="text-muted small">/ ${limit}</span></td>
                </tr>
            `);
        }
        container.innerHTML = `
            <table class="table table-sm mb-0">
                <thead>
                    <tr class="small text-muted">
                        <th style="width:3rem;">Bin</th>
                        <th>Query</th>
                        <th class="text-end" style="width:7rem;">Cards</th>
                    </tr>
                </thead>
                <tbody>${rows.join('')}</tbody>
            </table>
        `;
    } catch (e) {
        container.innerHTML = `<span class="text-danger small">Failed to load: ${e.message || e}</span>`;
    }
}

function _enrichSourceRow(name) {
    return document.querySelector(`[data-enrich-source="${CSS.escape(name)}"]`);
}

function _enrichShowProgress(name, {progress, total, message, started} = {}) {
    const row = _enrichSourceRow(name);
    if (!row) return;
    const wrap = row.querySelector('[data-enrich-progress]');
    const bar  = row.querySelector('[data-enrich-bar]');
    const msg  = row.querySelector('[data-enrich-msg]');
    const btn  = row.querySelector('[data-enrich-refresh-btn]');
    const runBadge = row.querySelector('[data-enrich-running]');
    if (wrap) wrap.style.display = '';
    if (btn) btn.disabled = true;
    if (runBadge) runBadge.innerHTML = ' <span class="badge bg-warning">running</span>';
    if (bar) {
        if (total && progress != null) {
            const pct = Math.max(0, Math.min(100, Math.round((progress / total) * 100)));
            bar.style.width = pct + '%';
            bar.setAttribute('aria-valuenow', String(pct));
            bar.textContent = '';
        } else if (started) {
            bar.style.width = '100%';
            bar.classList.add('progress-bar-striped', 'progress-bar-animated');
        }
    }
    if (msg) {
        const label = message || 'Working…';
        msg.textContent = (total && progress != null)
            ? `${label} (${progress}/${total})`
            : label;
    }
}

function _enrichFinishProgress(name, data) {
    const row = _enrichSourceRow(name);
    if (!row) return;
    const wrap = row.querySelector('[data-enrich-progress]');
    const bar  = row.querySelector('[data-enrich-bar]');
    const msg  = row.querySelector('[data-enrich-msg]');
    const btn  = row.querySelector('[data-enrich-refresh-btn]');
    const errs = (data && data.errors) || [];
    const ok = errs.length === 0;
    if (bar) {
        bar.style.width = '100%';
        bar.classList.remove('progress-bar-striped', 'progress-bar-animated');
        bar.classList.add(ok ? 'bg-success' : 'bg-danger');
    }
    if (msg) {
        const parts = [];
        if (data && data.duration_ms != null) parts.push(`${(data.duration_ms / 1000).toFixed(1)}s`);
        if (data && data.rows_changed != null) parts.push(`${data.rows_changed} rows`);
        if (!ok) parts.push(`${errs.length} error${errs.length === 1 ? '' : 's'}`);
        msg.textContent = (ok ? 'Done' : 'Failed') + (parts.length ? ' · ' + parts.join(' · ') : '');
    }
    if (btn) btn.disabled = false;
    // Reload sources so status/coverage reflects the new run, then hide the bar.
    setTimeout(() => {
        loadEnrichmentSources();
    }, 1500);
}

async function triggerEnrichmentRefresh(name) {
    // Show indeterminate progress immediately — the server returns 202
    // and the first socket event may take a moment.
    _enrichShowProgress(name, {started: true, message: 'Queued…'});
    try {
        await apiPost(`/api/enrichment/refresh/${encodeURIComponent(name)}`, {});
    } catch (e) {
        console.warn('refresh failed', name, e);
        _enrichFinishProgress(name, {errors: [String(e.message || e)]});
    }
}
