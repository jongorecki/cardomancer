// locator.js — Card locator
// ========================================================================
// Locator view (Phase 2.11)
// ========================================================================

async function runLocatorQuery() {
    const queryInput = document.getElementById('locator-query');
    const q = (queryInput?.value || '').trim();

    const status  = document.getElementById('locator-status');
    const empty   = document.getElementById('locator-empty');
    const errDiv  = document.getElementById('locator-error');
    const results = document.getElementById('locator-results');
    const total   = document.getElementById('locator-total');
    const echo    = document.getElementById('locator-query-echo');

    if (!q) {
        if (errDiv) { errDiv.textContent = 'Enter a query first.'; errDiv.style.display = ''; }
        return;
    }

    [status, empty, errDiv, results].forEach(el => { if (el) el.style.display = 'none'; });
    if (status) { status.style.display = ''; status.textContent = 'Searching…'; }
    if (total)  total.textContent = '0';
    if (echo)   echo.textContent  = '';

    try {
        const resp = await fetch('/api/locator/query', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ q }),
        });
        const data = await resp.json();
        if (status) status.style.display = 'none';

        if (!resp.ok) {
            if (errDiv) { errDiv.textContent = data.error || 'Server error'; errDiv.style.display = ''; }
            return;
        }

        if (echo) echo.textContent = `“${data.query}”`;
        if (total) total.textContent = data.total_cards || 0;

        if (!data.groups || data.groups.length === 0) {
            if (empty) empty.style.display = '';
            return;
        }

        if (results) {
            results.innerHTML = _renderLocatorGroups(data.groups);
            results.style.display = '';
        }
    } catch (e) {
        if (status) status.style.display = 'none';
        if (errDiv) { errDiv.textContent = 'Request failed: ' + e.message; errDiv.style.display = ''; }
    }
}

function _renderLocatorGroups(groups) {
    // Groups are already sorted: box asc, divider position asc, Unassigned last.
    const byBox = {};
    const boxOrder = [];
    for (const g of groups) {
        if (!byBox[g.box_name]) {
            byBox[g.box_name] = [];
            boxOrder.push(g.box_name);
        }
        byBox[g.box_name].push(g);
    }

    let html = '';
    for (const boxName of boxOrder) {
        const boxGroups = byBox[boxName];
        const boxTotal  = boxGroups.reduce((s, g) => s + (g.count || 0), 0);
        const boxId     = boxGroups[0]?.box_id;
        const boxBadge  = boxId ? `<span class="badge bg-secondary ms-1 small">${boxTotal} card${boxTotal !== 1 ? 's' : ''}</span>` : `<span class="badge bg-secondary ms-1 small">${boxTotal}</span>`;

        html += `<div class="mb-3">
<div class="d-flex align-items-center gap-2 mb-1">
  <strong>${_esc(boxName)}</strong>${boxBadge}
</div>`;

        for (const g of boxGroups) {
            const divLabel = g.divider_label
                ? `<span class="text-muted small ms-2">/ ${_esc(g.divider_label)}</span>`
                : '';
            const cardWord = g.count === 1 ? 'card' : 'cards';
            html += `<div class="border rounded p-2 mb-1 ms-3">
  <div class="d-flex justify-content-between align-items-center">
    <span>${divLabel || '<span class="text-muted small">(no divider)</span>'}
    </span>
    <span class="badge bg-primary">${g.count} ${cardWord}</span>
  </div>
  <div class="text-muted small mt-1">${g.unique_cards} unique oracle ID${g.unique_cards !== 1 ? 's' : ''}</div>
</div>`;
        }

        html += `</div>`;
    }
    return html;
}

function clearLocatorResults() {
    const fields = ['locator-results', 'locator-status', 'locator-empty', 'locator-error'];
    fields.forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.style.display = 'none'; el.innerHTML = ''; }
    });
    const total = document.getElementById('locator-total');
    if (total) total.textContent = '0';
    const echo = document.getElementById('locator-query-echo');
    if (echo) echo.textContent = '';
    const input = document.getElementById('locator-query');
    if (input) input.value = '';
}
