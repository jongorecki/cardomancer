// collection.js — Inventory + search + filters + manual add + autocomplete
// =========================================================================
// Collection
// =========================================================================

let _inventoryPage = 1;
let _inventorySort = 'name';
let _inventorySortDir = 'ASC';

async function loadCollectionStats() {
    try {
        const data = await apiGet('/api/collection/stats');
        document.getElementById('collection-stats').innerHTML = `
            <p class="mb-1">Unique cards: <strong>${data.unique_cards}</strong></p>
            <p class="mb-1">Total cards: <strong>${data.total_cards}</strong></p>
            <p class="mb-0">Total value: <strong>$${(data.total_value || 0).toFixed(2)}</strong></p>
        `;
    } catch (e) {
        if (typeof renderEmptyState === 'function') {
            renderEmptyState('collection-stats', {
                sigil: 'card',
                title: 'No database yet.',
                body: 'Run a sort session to start logging cards into the collection.',
                variant: 'compact',
            });
        } else {
            document.getElementById('collection-stats').innerHTML =
                '<p class="text-muted">No database found</p>';
        }
    }
}

async function loadBoxSummary() {
    try {
        const data = await apiGet('/api/collection/boxes/summary');
        const card = document.getElementById('box-summary-card');
        const body = document.getElementById('box-summary-body');
        if (!data.boxes || data.boxes.length === 0) {
            card.style.display = 'none';
            return;
        }
        card.style.display = 'block';
        let html = '<table class="table table-sm mb-0"><tbody>';
        for (const b of data.boxes) {
            const name = b.box_name === '__unassigned__' ? '<em>Unassigned</em>' : b.box_name;
            const val = b.total_value > 0 ? ` · $${b.total_value.toFixed(2)}` : '';
            html += `<tr><td class="small">${name}</td><td class="small text-end text-nowrap">${b.total_cards} cards${val}</td></tr>`;
        }
        html += '</tbody></table>';
        body.innerHTML = html;
    } catch (e) {}
}

function _priceClass(price) {
    if (!price) return '';
    if (price >= 50) return 'price-mythic';
    if (price >= 20) return 'price-rare';
    if (price >= 5) return 'price-uncommon';
    return '';
}

function _renderInventoryRow(item) {
    const price = item.price_usd ? `$${item.price_usd.toFixed(2)}` : 'N/A';
    const priceClass = _priceClass(item.price_usd);
    const boxDisplay = item.box || '<span class="text-secondary">—</span>';
    const removeBtns = item.quantity > 1
        ? `<button class="btn btn-outline-warning btn-sm py-0 px-1" onclick="deleteInventoryItem(${item.id}, 1)" title="Remove 1 copy">&minus;1</button>
           <button class="btn btn-outline-danger btn-sm py-0 px-1" onclick="deleteInventoryItem(${item.id})" title="Remove all copies">&times;</button>`
        : `<button class="btn btn-outline-danger btn-sm py-0 px-1" onclick="deleteInventoryItem(${item.id})" title="Remove">&times;</button>`;
    const imgUrl = item.set_code && item.collector_number
        ? _scryfallImageUrl(item.set_code, item.collector_number, 'normal')
        : '';
    const hoverAttr = imgUrl ? `onmouseenter="_showCardPreview(event, '${imgUrl}')" onmouseleave="_hideCardPreview()"` : '';
    const scryfallLink = item.set_code && item.collector_number
        ? `<a href="https://scryfall.com/card/${item.set_code}/${item.collector_number}" target="_blank" rel="noopener" class="text-decoration-none" title="View on Scryfall">${item.set_code.toUpperCase()}</a>`
        : item.set_code;
    return `<tr class="${priceClass}">
        <td><input type="checkbox" class="inv-select" value="${item.id}" onchange="_updateBulkCount()"></td>
        <td class="card-hover-name" style="cursor:default" ${hoverAttr}>${item.name}</td>
        <td>${scryfallLink}</td><td>${item.colors || ''}</td>
        <td class="small">${item.type_line || ''}</td><td>${item.rarity || ''}</td>
        <td>${price}</td><td>${item.quantity}</td>
        <td><span class="box-label" style="cursor:pointer" onclick="showBoxAssign(${item.id}, ${item.quantity})" title="Click to assign box">${boxDisplay}</span></td>
        <td class="text-nowrap">
            <button class="btn btn-outline-success btn-sm py-0 px-1" onclick="incrementInventoryItem(${item.id})" title="Add 1 copy">+1</button>
            ${removeBtns}
        </td>
    </tr>`;
}

async function loadInventory(page) {
    _inventoryPage = page || 1;
    try {
        // If any filter chips are active OR free-text query is present,
        // hit the filter endpoint with the compiled query; otherwise
        // fall back to the raw inventory endpoint.
        const q = _compileCollectionQuery();
        let url;
        if (q) {
            url = `/api/collection/filter?q=${encodeURIComponent(q)}&page=${_inventoryPage}&per_page=50&sort=${_inventorySort}&dir=${_inventorySortDir}`;
        } else {
            url = `/api/collection/inventory?page=${_inventoryPage}&per_page=50&sort=${_inventorySort}&dir=${_inventorySortDir}`;
        }
        const data = await apiGet(url);
        const errEl = document.getElementById('collection-filter-error');
        if (errEl) {
            if (data && data.error) {
                errEl.textContent = data.error;
                errEl.style.display = '';
            } else {
                errEl.textContent = '';
                errEl.style.display = 'none';
            }
        }
        document.getElementById('inventory-count').textContent = data.total || 0;
        const tbody = document.getElementById('inventory-body');
        tbody.innerHTML = '';
        for (const item of data.items || []) {
            tbody.innerHTML += _renderInventoryRow(item);
        }
        _updateSortArrows();
        _renderInventoryPagination(data.pages || 0, data.page || 1);
    } catch (e) {
        const errEl = document.getElementById('collection-filter-error');
        if (errEl && e && e.message) {
            errEl.textContent = e.message;
            errEl.style.display = '';
        }
    }
}

/**
 * Compact pagination for the inventory table.
 *
 * The original implementation rendered one button per page — fine at
 * 10 pages, hostile at 200+ (the inventory grows fast on a real
 * sorter). This replacement shows: « prev / first / … / N-1 [N] N+1
 * / … / last / next » so the DOM stays bounded at ~9 buttons even
 * with thousands of pages.
 *
 * Server-side pagination at /api/collection/filter handles the
 * actual row-window math; this is purely the click target UI.
 */
function _renderInventoryPagination(totalPages, currentPage) {
    const div = document.getElementById('inventory-pagination');
    if (!div) return;
    div.innerHTML = '';
    if (totalPages <= 1) return;

    // Window of pages to render around the current one.
    const WINDOW = 2;
    const pages = new Set([1, totalPages, currentPage]);
    for (let d = 1; d <= WINDOW; d++) {
        if (currentPage - d > 1) pages.add(currentPage - d);
        if (currentPage + d < totalPages) pages.add(currentPage + d);
    }
    const sorted = Array.from(pages).sort((a, b) => a - b);

    const make = (label, page, opts = {}) => {
        const btn = document.createElement('button');
        const variant = opts.active ? 'btn-primary' : 'btn-outline-secondary';
        btn.className = `btn btn-sm ${variant} mx-1`;
        btn.textContent = label;
        if (opts.disabled) {
            btn.disabled = true;
        } else if (page != null) {
            btn.onclick = () => loadInventory(page);
        }
        if (opts.title) btn.title = opts.title;
        return btn;
    };

    // Prev arrow
    div.appendChild(make('«', currentPage - 1, {
        disabled: currentPage <= 1,
        title: 'Previous page',
    }));

    // Numbered pages with ellipses where the window skips
    let last = 0;
    for (const p of sorted) {
        if (p > last + 1) {
            const ell = document.createElement('span');
            ell.className = 'mx-1 text-muted';
            ell.textContent = '…';
            div.appendChild(ell);
        }
        div.appendChild(make(String(p), p, { active: p === currentPage }));
        last = p;
    }

    // Next arrow
    div.appendChild(make('»', currentPage + 1, {
        disabled: currentPage >= totalPages,
        title: 'Next page',
    }));
}

// ---- Collection tab filter chips (2.13) ----

/**
 * Walk the Filters card and compile the active chip selections into a
 * query string. Within a group chips are ORed; groups are ANDed.
 * Free-text from #collection-filter-raw is ANDed at the end.
 * Returns '' when no filters are active.
 */
function _compileCollectionQuery() {
    const groups = document.querySelectorAll('#collection-filters-card [data-filter-group]');
    const groupFrags = [];
    groups.forEach(grp => {
        const active = grp.querySelectorAll('.chip.active');
        if (!active.length) return;
        // "Any staple" overrides the others if selected.
        let exclusive = null;
        active.forEach(c => { if (c.dataset.chipExclusive === '1') exclusive = c; });
        const chips = exclusive ? [exclusive] : Array.from(active);
        const tokens = chips.map(c => c.dataset.chipToken).filter(Boolean);
        if (!tokens.length) return;
        if (tokens.length === 1) {
            groupFrags.push(tokens[0]);
        } else {
            groupFrags.push('(' + tokens.join(' or ') + ')');
        }
    });
    const rawInput = document.getElementById('collection-filter-raw');
    const raw = rawInput ? rawInput.value.trim() : '';
    if (raw) groupFrags.push(raw);
    return groupFrags.join(' ');
}

/** Refresh the compiled-query preview and reload the inventory list. */
function updateCollectionFilter() {
    const compiled = _compileCollectionQuery();
    const preview = document.getElementById('collection-filter-compiled');
    if (preview) preview.textContent = compiled || '(none)';
    loadInventory(1);
}

/** Clear every chip + free text, then reload. */
function clearCollectionFilters() {
    document.querySelectorAll('#collection-filters-card .chip.active').forEach(c => {
        c.classList.remove('active');
    });
    const rawInput = document.getElementById('collection-filter-raw');
    if (rawInput) rawInput.value = '';
    updateCollectionFilter();
}

/** Toggle a chip on click; enforce exclusive chips within a group. */
function _initCollectionFilterChips() {
    const card = document.getElementById('collection-filters-card');
    if (!card) return;
    card.querySelectorAll('.chip').forEach(chip => {
        chip.addEventListener('click', (ev) => {
            ev.preventDefault();
            const group = chip.closest('[data-filter-group]');
            const wasActive = chip.classList.contains('active');
            if (chip.dataset.chipExclusive === '1') {
                // Exclusive chip: toggle it and clear siblings.
                if (group) {
                    group.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
                }
                if (!wasActive) chip.classList.add('active');
            } else {
                // Non-exclusive: toggle it; if an exclusive sibling is
                // active, clear it (the two can't coexist).
                if (group) {
                    group.querySelectorAll('.chip[data-chip-exclusive="1"].active')
                         .forEach(c => c.classList.remove('active'));
                }
                chip.classList.toggle('active');
            }
            updateCollectionFilter();
        });
    });
    // Initial preview render.
    const preview = document.getElementById('collection-filter-compiled');
    if (preview) preview.textContent = '(none)';
}

if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _initCollectionFilterChips);
    } else {
        _initCollectionFilterChips();
    }
}

function _updateSortArrows() {
    document.querySelectorAll('#tab-collection th.sortable').forEach(th => {
        const arrow = th.querySelector('.sort-arrow');
        if (th.dataset.sort === _inventorySort) {
            arrow.textContent = _inventorySortDir === 'ASC' ? ' ▲' : ' ▼';
        } else {
            arrow.textContent = '';
        }
    });
}

function sortInventory(column) {
    if (_inventorySort === column) {
        _inventorySortDir = _inventorySortDir === 'ASC' ? 'DESC' : 'ASC';
    } else {
        _inventorySort = column;
        _inventorySortDir = 'ASC';
    }
    loadInventory(1);
}

async function searchCollection() {
    const params = new URLSearchParams();
    const name = document.getElementById('search-name').value;
    const set = document.getElementById('search-set').value;
    const colors = document.getElementById('search-colors').value;
    const rarity = document.getElementById('search-rarity').value;
    const box = document.getElementById('search-box').value;
    if (name) params.set('name', name);
    if (set) params.set('set', set);
    if (colors) params.set('colors', colors);
    if (rarity) params.set('rarity', rarity);
    if (box) params.set('box', box);
    params.set('sort', _inventorySort);
    params.set('dir', _inventorySortDir);

    try {
        const data = await apiGet(`/api/collection/inventory?${params.toString()}`);
        document.getElementById('inventory-count').textContent = data.total;
        const tbody = document.getElementById('inventory-body');
        tbody.innerHTML = '';
        for (const item of data.items || []) {
            tbody.innerHTML += _renderInventoryRow(item);
        }
        _updateSortArrows();
    } catch (e) {}
}

async function incrementInventoryItem(itemId) {
    await fetch(`/api/collection/inventory/${itemId}/increment`, { method: 'POST' });
    loadInventory(_inventoryPage);
    loadCollectionStats();
}

async function deleteInventoryItem(itemId, qty) {
    if (!qty && !confirm('Remove all copies of this card?')) return;
    const url = qty
        ? `/api/collection/inventory/${itemId}?qty=${qty}`
        : `/api/collection/inventory/${itemId}`;
    await fetch(url, { method: 'DELETE' });
    loadInventory(_inventoryPage);
    loadCollectionStats();
}

// ---- Add Card with autocomplete ----

let _addCardDebounce = null;
let _addCardPrintings = [];  // cached printings for currently selected card

function _initAddCardAutocomplete() {
    const input = document.getElementById('add-card-name');
    const sugBox = document.getElementById('add-card-suggestions');

    input.addEventListener('input', () => {
        clearTimeout(_addCardDebounce);
        const q = input.value.trim();
        if (q.length < 2) { sugBox.style.display = 'none'; return; }
        _addCardDebounce = setTimeout(async () => {
            try {
                const data = await apiGet(`/api/cards/autocomplete?q=${encodeURIComponent(q)}`);
                if (!data.suggestions || data.suggestions.length === 0) {
                    sugBox.style.display = 'none';
                    return;
                }
                sugBox.innerHTML = data.suggestions.map(name =>
                    `<button type="button" class="list-group-item list-group-item-action py-1 px-2 small"
                        onclick="_selectCardName('${name.replace(/'/g, "\\'")}')">${name}</button>`
                ).join('');
                sugBox.style.display = 'block';
            } catch (e) { sugBox.style.display = 'none'; }
        }, 250);
    });

    // Hide suggestions when clicking outside
    document.addEventListener('click', (e) => {
        if (!input.contains(e.target) && !sugBox.contains(e.target)) {
            sugBox.style.display = 'none';
        }
    });

    // Update printing info when set changes
    document.getElementById('add-card-set').addEventListener('change', _updatePrintingInfo);
}

async function _selectCardName(name) {
    const input = document.getElementById('add-card-name');
    const sugBox = document.getElementById('add-card-suggestions');
    const setSelect = document.getElementById('add-card-set');

    input.value = name;
    sugBox.style.display = 'none';

    // Fetch printings for this card
    try {
        const data = await apiGet(`/api/cards/printings?name=${encodeURIComponent(name)}`);
        _addCardPrintings = data.printings || [];
        setSelect.innerHTML = '';
        if (_addCardPrintings.length === 0) {
            setSelect.innerHTML = '<option value="">No printings found</option>';
            setSelect.disabled = true;
        } else {
            for (const p of _addCardPrintings) {
                const priceStr = p.price_usd ? ` — $${p.price_usd.toFixed(2)}` : '';
                const opt = document.createElement('option');
                opt.value = `${p.set}|${p.collector_number}`;
                opt.textContent = `${p.set.toUpperCase()} #${p.collector_number} (${p.rarity})${priceStr}`;
                setSelect.appendChild(opt);
            }
            setSelect.disabled = false;
        }
        _updatePrintingInfo();
    } catch (e) {
        setSelect.innerHTML = '<option value="">Error loading printings</option>';
        setSelect.disabled = true;
    }
}

function _scryfallImageUrl(set, collectorNumber, version) {
    return `https://api.scryfall.com/cards/${encodeURIComponent(set)}/${encodeURIComponent(collectorNumber)}?format=image&version=${version || 'normal'}`;
}

function _showCardPreview(event, imgUrl, side) {
    let el = document.getElementById('card-hover-preview');
    if (!el) {
        el = document.createElement('div');
        el.id = 'card-hover-preview';
        el.style.cssText = 'position:fixed;z-index:9999;pointer-events:none;display:none;filter:drop-shadow(0 4px 12px rgba(0,0,0,0.7));transition:opacity 0.15s';
        el.innerHTML = '<img style="max-height:340px;border-radius:12px">';
        document.body.appendChild(el);
    }
    const img = el.querySelector('img');
    img.src = imgUrl;
    el.style.display = 'block';
    el.style.opacity = '0';
    img.onload = () => { el.style.opacity = '1'; };

    // Position: default is the right edge of the viewport (used by the
    // collection inventory table). Callers with content on the right
    // side of the page (e.g. the sort-session bin panels) pass
    // side='left' so the preview hugs the left edge instead of
    // covering the bin list.
    const y = event.clientY - 100;
    const maxY = window.innerHeight - 370;
    if (side === 'left') {
        const anchor = event.target.closest('.bin-section');
        if (anchor) {
            const rect = anchor.getBoundingClientRect();
            el.style.left = Math.max(8, rect.left - 258) + 'px';
        } else {
            el.style.left = '8px';
        }
    } else {
        el.style.left = (window.innerWidth - 270) + 'px';
    }
    el.style.top = Math.max(8, Math.min(y, maxY)) + 'px';

    // Highlight the row (inventory table uses a <tr>; other callers
    // just get the preview without the highlight).
    const row = event.target.closest('tr');
    if (row) row.classList.add('inventory-row-hover');
}

function _hideCardPreview() {
    const el = document.getElementById('card-hover-preview');
    if (el) {
        el.style.display = 'none';
        el.querySelector('img').src = '';
    }
    // Remove highlight from all rows
    document.querySelectorAll('.inventory-row-hover').forEach(r => r.classList.remove('inventory-row-hover'));
}

function _updatePrintingInfo() {
    const infoEl = document.getElementById('add-card-printing-info');
    const imageEl = document.getElementById('add-card-image');
    const imgTag = document.getElementById('add-card-img');
    const val = document.getElementById('add-card-set').value;
    if (!val || _addCardPrintings.length === 0) {
        infoEl.style.display = 'none';
        imageEl.style.display = 'none';
        return;
    }
    const [set, num] = val.split('|');
    const p = _addCardPrintings.find(x => x.set === set && x.collector_number === num);
    if (p) {
        infoEl.style.display = 'block';
        infoEl.textContent = `${p.type_line || ''} · ${p.colors || 'Colorless'}`;
        imgTag.src = _scryfallImageUrl(set, num, 'normal');
        imgTag.alt = document.getElementById('add-card-name').value;
        imageEl.style.display = 'block';
    } else {
        infoEl.style.display = 'none';
        imageEl.style.display = 'none';
    }
}

async function addManualCard() {
    const name = document.getElementById('add-card-name').value.trim();
    const setVal = document.getElementById('add-card-set').value;
    if (!name) {
        alert('Select a card name from the suggestions.');
        return;
    }
    if (!setVal) {
        alert('Select a set/printing.');
        return;
    }

    const [set_code, collector_number] = setVal.split('|');
    const printing = _addCardPrintings.find(
        p => p.set === set_code && p.collector_number === collector_number
    );

    const statusEl = document.getElementById('add-card-status');
    statusEl.style.display = 'block';
    statusEl.className = 'small mt-1 text-info';
    statusEl.textContent = 'Adding...';

    try {
        const resp = await fetch('/api/collection/inventory', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name,
                set_code,
                collector_number: collector_number || '',
                colors: printing?.colors || null,
                type_line: printing?.type_line || null,
                rarity: printing?.rarity || null,
                price_usd: printing?.price_usd || null,
                quantity: parseInt(document.getElementById('add-card-qty').value) || 1,
            }),
        });
        const data = await resp.json();
        if (resp.ok) {
            statusEl.className = 'small mt-1 text-success';
            statusEl.textContent = `Added "${name}" (${set_code.toUpperCase()}) to collection`;
            document.getElementById('add-card-name').value = '';
            document.getElementById('add-card-set').innerHTML = '<option value="">Select a card first</option>';
            document.getElementById('add-card-set').disabled = true;
            document.getElementById('add-card-printing-info').style.display = 'none';
            document.getElementById('add-card-image').style.display = 'none';
            document.getElementById('add-card-qty').value = '1';
            _addCardPrintings = [];
            loadInventory(_inventoryPage);
            loadCollectionStats();
        } else {
            statusEl.className = 'small mt-1 text-danger';
            statusEl.textContent = data.error || 'Failed to add card';
        }
    } catch (e) {
        statusEl.className = 'small mt-1 text-danger';
        statusEl.textContent = 'Failed: ' + e.message;
    }
}

async function deleteSession(sessionId) {
    if (!confirm(`Delete session #${sessionId} and all its scan history?`)) return;
    await fetch(`/api/collection/sessions/${sessionId}`, { method: 'DELETE' });
    loadSessionHistory();
    loadCollectionStats();
}

async function resetCollection() {
    if (!confirm('This will permanently delete ALL cards, sessions, and scan history. Are you sure?')) return;
    if (!confirm('This cannot be undone. Click OK to confirm.')) return;
    try {
        await apiPost('/api/collection/reset');
        loadCollectionStats();
        loadInventory();
        loadSessionHistory();
        loadBoxList();
        addLog('Collection reset — all data deleted');
    } catch (e) {
        addLog('Failed to reset collection: ' + e.message);
    }
}

async function importCollectionCSV(input) {
    if (!input.files || !input.files[0]) return;
    const file = input.files[0];
    const statusEl = document.getElementById('import-status');
    statusEl.style.display = 'block';
    statusEl.className = 'small mt-1 text-info';
    statusEl.textContent = `Importing ${file.name}...`;

    const formData = new FormData();
    formData.append('file', file);

    try {
        const resp = await fetch('/api/collection/import/csv', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();
        if (resp.ok) {
            statusEl.className = 'small mt-1 text-success';
            statusEl.textContent = `Imported: ${data.imported} new, ${data.updated} updated, ${data.skipped} skipped`;
            loadCollectionStats();
            loadInventory();
            addLog(`CSV imported: ${data.total} cards processed`);
        } else {
            statusEl.className = 'small mt-1 text-danger';
            statusEl.textContent = data.error || 'Import failed';
        }
    } catch (e) {
        statusEl.className = 'small mt-1 text-danger';
        statusEl.textContent = 'Import failed: ' + e.message;
    }
    // Reset file input so the same file can be re-imported
    input.value = '';
}

// ---- Bulk selection ----

function toggleSelectAll(master) {
    document.querySelectorAll('.inv-select').forEach(cb => { cb.checked = master.checked; });
    _updateBulkCount();
}

function _updateBulkCount() {
    const checked = document.querySelectorAll('.inv-select:checked').length;
    const bar = document.getElementById('bulk-actions');
    bar.style.display = checked > 0 ? 'flex' : 'none';
    document.getElementById('bulk-count').textContent = checked;
}

function bulkClearSelection() {
    document.querySelectorAll('.inv-select').forEach(cb => { cb.checked = false; });
    document.getElementById('select-all-inv').checked = false;
    _updateBulkCount();
}

function wireCollectionSubnav() {
    const nav = document.getElementById('collection-subnav');
    if (!nav) return;
    nav.querySelectorAll('a[data-collection-view]').forEach(link => {
        link.addEventListener('click', (ev) => {
            ev.preventDefault();
            nav.querySelectorAll('a.nav-link').forEach(a => a.classList.remove('active'));
            link.classList.add('active');
            const view = link.getAttribute('data-collection-view');
            document.querySelectorAll('[data-collection-section]').forEach(sec => {
                sec.style.display = (sec.getAttribute('data-collection-section') === view)
                    ? '' : 'none';
            });
            // Trigger data loads for views that need it
            if (view === 'cull') loadCullCandidates();
        });
    });
}
