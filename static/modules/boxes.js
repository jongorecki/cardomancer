// boxes.js — Box assignment

async function bulkBoxAssign() {
    const ids = [...document.querySelectorAll('.inv-select:checked')].map(cb => parseInt(cb.value));
    if (ids.length === 0) return;

    // Reuse the box assign overlay but for bulk
    let boxes = [];
    try { const data = await apiGet('/api/collection/boxes'); boxes = data.boxes || []; } catch (e) {}
    const boxListHtml = boxes.map(b => `<option value="${b}">`).join('');

    const overlay = document.createElement('div');
    overlay.id = 'box-assign-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;z-index:9999';
    overlay.innerHTML = `
        <div style="background:var(--bg-card,#1c1f26);border:1px solid var(--border,#2d3240);border-radius:8px;padding:20px;min-width:320px;color:var(--text-primary,#e8eaf0)">
            <h6>Assign ${ids.length} cards to Box</h6>
            <input type="text" class="form-control form-control-sm mb-2" id="bulk-box-name"
                   placeholder="Box name" list="bulk-box-list" autocomplete="off">
            <datalist id="bulk-box-list">${boxListHtml}</datalist>
            <div class="d-flex gap-2">
                <button class="btn btn-primary btn-sm" id="bulk-box-confirm">Assign</button>
                <button class="btn btn-outline-secondary btn-sm" id="bulk-box-remove">Remove from box</button>
                <button class="btn btn-outline-secondary btn-sm" onclick="document.getElementById('box-assign-overlay').remove()">Cancel</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

    const doAssign = async (boxName) => {
        for (const id of ids) {
            await fetch(`/api/collection/inventory/${id}/box`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ box: boxName }),
            });
        }
        overlay.remove();
        bulkClearSelection();
        loadInventory(_inventoryPage);
        loadBoxList();
        loadBoxSummary();
    };

    document.getElementById('bulk-box-confirm').onclick = () => {
        const name = document.getElementById('bulk-box-name').value.trim();
        if (!name) { alert('Enter a box name'); return; }
        doAssign(name);
    };
    document.getElementById('bulk-box-remove').onclick = () => doAssign('');
    document.getElementById('bulk-box-name').focus();
}

// ---- Box management ----

async function loadBoxList() {
    try {
        const data = await apiGet('/api/collection/boxes');
        const sel = document.getElementById('search-box');
        // Keep first two options (Any box, Unassigned)
        while (sel.options.length > 2) sel.remove(2);
        for (const box of data.boxes || []) {
            const opt = document.createElement('option');
            opt.value = box;
            opt.textContent = box;
            sel.appendChild(opt);
        }
    } catch (e) {}
}

async function showBoxAssign(itemId, quantity) {
    // Fetch current box list for datalist
    let boxes = [];
    try {
        const data = await apiGet('/api/collection/boxes');
        boxes = data.boxes || [];
    } catch (e) {}

    const boxListHtml = boxes.map(b => `<option value="${b}">`).join('');

    // Build a small inline form — use a modal-like overlay
    const overlay = document.createElement('div');
    overlay.id = 'box-assign-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;z-index:9999';
    overlay.innerHTML = `
        <div style="background:var(--bg-card,#1c1f26);border:1px solid var(--border,#2d3240);border-radius:8px;padding:20px;min-width:320px;color:var(--text-primary,#e8eaf0)">
            <h6>Assign to Box</h6>
            <input type="text" class="form-control form-control-sm mb-2" id="box-assign-name"
                   placeholder="Box name (e.g. Red Box, Trade Binder)" list="box-assign-list" autocomplete="off">
            <datalist id="box-assign-list">${boxListHtml}</datalist>
            ${quantity > 1 ? `
            <label class="form-label small">Move how many? (total: ${quantity})</label>
            <input type="number" class="form-control form-control-sm mb-2" id="box-assign-qty"
                   value="${quantity}" min="1" max="${quantity}">
            ` : ''}
            <div class="d-flex gap-2">
                <button class="btn btn-primary btn-sm" onclick="confirmBoxAssign(${itemId}, ${quantity})">Assign</button>
                <button class="btn btn-outline-secondary btn-sm" onclick="confirmBoxAssign(${itemId}, ${quantity}, true)">Remove from box</button>
                <button class="btn btn-outline-secondary btn-sm" onclick="document.getElementById('box-assign-overlay').remove()">Cancel</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    document.getElementById('box-assign-name').focus();
}

async function confirmBoxAssign(itemId, totalQty, removeBox) {
    const overlay = document.getElementById('box-assign-overlay');
    const boxName = removeBox ? '' : (document.getElementById('box-assign-name')?.value || '').trim();
    const qtyEl = document.getElementById('box-assign-qty');
    const moveQty = qtyEl ? parseInt(qtyEl.value) : null;

    if (!removeBox && !boxName) {
        alert('Enter a box name or click "Remove from box"');
        return;
    }

    const body = { box: boxName };
    if (moveQty !== null && moveQty < totalQty) {
        body.quantity = moveQty;
    }

    await fetch(`/api/collection/inventory/${itemId}/box`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });

    if (overlay) overlay.remove();
    loadInventory(_inventoryPage);
    loadBoxList();
}
