// cull.js — Cull view
// ========================================================================
// Cull candidates view
// ========================================================================

async function loadCullCandidates() {
    const maxPrice   = parseFloat(document.getElementById('cull-max-price')?.value) || 1.0;
    const maxBuylist = parseFloat(document.getElementById('cull-max-buylist')?.value) ?? 0.05;
    const preset     = document.getElementById('cull-preset')?.value || 'default';
    const status  = document.getElementById('cull-status');
    const empty   = document.getElementById('cull-empty');
    const table   = document.getElementById('cull-table');
    const tbody   = document.getElementById('cull-body');
    const count   = document.getElementById('cull-count');

    if (!status) return;

    status.textContent   = 'Loading…';
    status.style.display = '';
    empty.style.display  = 'none';
    table.style.display  = 'none';
    tbody.innerHTML      = '';
    count.textContent    = '0';

    try {
        const url  = `/api/collection/cull-candidates?max_price=${maxPrice}&max_buylist=${maxBuylist}&preset=${encodeURIComponent(preset)}`;
        const data  = await apiGet(url);
        const items = data.candidates || [];

        count.textContent    = items.length;
        status.style.display = 'none';

        if (items.length === 0) {
            empty.style.display = '';
            return;
        }

        const actionBadge = {
            donate: 'bg-secondary',
            bulk:   'bg-warning text-dark',
            trade:  'bg-info text-dark',
            sell:   'bg-success',
        };

        tbody.innerHTML = items.map(c => {
            const price    = c.price_usd   != null ? '$' + parseFloat(c.price_usd).toFixed(2)   : '—';
            const buylist  = c.buylist_price != null ? '$' + parseFloat(c.buylist_price).toFixed(2) : '—';
            const salt     = c.salt_score   != null ? parseFloat(c.salt_score).toFixed(1) : '—';
            const action   = c.suggested_action || 'bulk';
            const badgeCls = actionBadge[action] || 'bg-secondary';
            const location = c.location || c.box || '—';
            const reasons  = (c.cull_reasons || [])
                .map(r => `<span class="badge bg-secondary me-1">${r}</span>`)
                .join('');
            return `<tr>
                <td>${c.name || ''}</td>
                <td><code>${c.set_code || ''}</code></td>
                <td class="small text-muted">${c.type_line || ''}</td>
                <td>${price}</td>
                <td>${buylist}</td>
                <td>${salt}</td>
                <td>${c.quantity ?? ''}</td>
                <td><span class="badge ${badgeCls}">${action}</span></td>
                <td class="small">${location}</td>
                <td>${reasons}</td>
            </tr>`;
        }).join('');

        table.style.display = '';
    } catch (e) {
        status.textContent = 'Error loading candidates: ' + e.message;
    }
}

function exportCullCSV() {
    const maxPrice   = parseFloat(document.getElementById('cull-max-price')?.value) || 1.0;
    const maxBuylist = parseFloat(document.getElementById('cull-max-buylist')?.value) ?? 0.05;
    const preset     = document.getElementById('cull-preset')?.value || 'default';
    window.location.href = `/api/collection/cull-candidates/export?max_price=${maxPrice}&max_buylist=${maxBuylist}&preset=${encodeURIComponent(preset)}`;
}
