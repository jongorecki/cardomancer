// sortConfig.js — Sort preset / query editor
// =========================================================================
// Sort Configuration (unified preset + override bins)
// =========================================================================

// BUILTIN_CONFIGS: filenames that ship with the project and are treated as
// read-only. The canonical list lives on the server (BUILTIN_SORT_PRESETS in
// web_server.py) and is returned by /api/sort/configs. We keep this Set
// populated from the server response — the literal below is only a seed so
// the UI behaves sanely on first render before the server list arrives.
let BUILTIN_CONFIGS = new Set([
    'color.txt', 'mana_value.txt', 'price.txt', 'price_tiers.txt',
    'set.txt', 'type.txt', 'color_type.txt', 'edh_staples.txt',
]);

let _scFilename = null;      // currently loaded filename (null = unsaved)
let _scDirty = false;        // any unsaved in-memory changes
let _scBuiltin = false;      // currently loaded file is a builtin (read-only)
let _scPresetMeta = {};      // filename → {description, bin_count, builtin}

// ---------- Config text parser ----------

function _parseSortConfigText(text) {
    const result = {
        description: '',
        bins: 10,
        fallback: 10,
        limit: null,
        overrides: new Set(),
        binQueries: {},
    };
    for (const raw of text.split('\n')) {
        const line = raw.trim();
        if (!line) continue;
        if (line.startsWith('#')) {
            if (!result.description) result.description = line.slice(1).trim();
            continue;
        }
        const m = line.match(/^(\w+):\s*(.*)$/);
        if (!m) continue;
        const key = m[1].toLowerCase();
        const val = m[2].trim();
        if (key === 'bins') result.bins = parseInt(val) || 10;
        else if (key === 'fallback') result.fallback = parseInt(val) || 10;
        else if (key === 'limit') result.limit = parseInt(val) || null;
        else if (key === 'overrides') {
            for (const n of val.split(',')) {
                const bn = parseInt(n.trim());
                if (bn) result.overrides.add(bn);
            }
        } else if (key.startsWith('bin') && key.length > 3) {
            const num = parseInt(key.slice(3));
            if (num) result.binQueries[num] = val;
        }
    }
    return result;
}

// ---------- Config text serializer ----------

function serializeSortConfig() {
    const bins = parseInt(document.getElementById('sc-bin-count').value) || 10;
    const fallback = parseInt(document.getElementById('sc-fallback').value) || bins;
    const limitVal = document.getElementById('sc-bin-limit').value.trim();
    const overrideNums = [];
    const tbody = document.getElementById('sort-config-tbody');
    if (!tbody) return '';
    const rows = tbody.querySelectorAll('tr[data-bin]');

    let text = '';
    if (_scFilename) {
        const name = _scFilename.replace(/\.txt$/, '');
        text += `# ${name}\n`;
    }
    text += `bins: ${bins}\n`;
    text += `fallback: ${fallback}\n`;
    if (limitVal) text += `limit: ${limitVal}\n`;

    const binLines = [];
    rows.forEach(tr => {
        const binNum = parseInt(tr.getAttribute('data-bin'));
        const queryInput = tr.querySelector('.sc-query-input');
        const overrideCb = tr.querySelector('.sc-override-cb');
        const query = queryInput ? queryInput.value.trim() : '';
        if (overrideCb && overrideCb.checked) overrideNums.push(binNum);
        if (query) binLines.push(`bin${binNum}: ${query}`);
    });

    if (overrideNums.length) text += `overrides: ${overrideNums.join(',')}\n`;
    text += binLines.join('\n');
    if (binLines.length) text += '\n';
    return text;
}

// ---------- Row rendering ----------

function _renderSortConfigRow(binNum, query, isOverride, isFallback) {
    const tr = document.createElement('tr');
    tr.setAttribute('data-bin', binNum);
    if (isOverride) tr.classList.add('bin-row', 'is-override');
    else tr.classList.add('bin-row');

    const binLabel = isFallback
        ? `<span class="bin-num">${binNum}</span> <span class="badge bg-secondary" style="font-size:0.65rem;">fb</span>`
        : `<span class="bin-num">${binNum}</span>`;

    const overrideDisabled = isFallback ? 'disabled' : '';
    const overrideChecked = isOverride ? 'checked' : '';
    const queryVal = escapeHtml(query || '');
    const queryPlaceholder = isFallback ? '(fallback — no query needed)' : 'e.g. c:w t:creature';

    tr.innerHTML = `
        <td class="text-center">${binLabel}</td>
        <td>
            <div class="input-group input-group-sm">
                <input type="text" class="form-control form-control-sm sc-query-input"
                       value="${queryVal}"
                       placeholder="${queryPlaceholder}"
                       ${isFallback ? 'disabled' : ''}
                       oninput="markSortConfigDirty(); _validateSortRow(this)">
                <button class="btn btn-outline-secondary btn-sm sc-scryfall-btn"
                        type="button"
                        ${isFallback ? 'disabled' : ''}
                        onclick="_openBinQueryOnScryfall(this)"
                        title="Preview matching cards on Scryfall (enrichment-only predicates dropped)">
                    🔗
                </button>
            </div>
        </td>
        <td class="text-center">
            <input type="checkbox" class="form-check-input sc-override-cb"
                   ${overrideChecked} ${overrideDisabled}
                   onchange="onOverrideToggle(${binNum}, this)">
        </td>
        <td class="text-center">
            <button class="btn btn-link btn-sm p-0 text-danger sc-remove-btn"
                    onclick="removeSortConfigRow(this)"
                    title="Remove bin">
                &times;
            </button>
        </td>
    `;
    return tr;
}

// Open this bin's query on Scryfall in a new tab. Translates our DSL ->
// Scryfall syntax via /api/translate/scryfall; predicates that don't map
// (staple:, salt:, buylist:, etc.) are dropped silently with a small alert
// listing what was lost so the user knows the preview is approximate.
async function _openBinQueryOnScryfall(btn) {
    const tr = btn.closest('tr');
    if (!tr) return;
    const inp = tr.querySelector('.sc-query-input');
    const dsl = (inp?.value || '').trim();
    if (!dsl) {
        alert('No query to preview — enter a bin query first.');
        return;
    }
    try {
        const resp = await fetch('/api/translate/scryfall?q=' + encodeURIComponent(dsl));
        const data = await resp.json();
        if (!data.url) {
            alert('Nothing in this query translates to Scryfall.\n\n'
                  + 'All predicates are enrichment-only or scan-time:\n  '
                  + (data.dropped || []).join('\n  '));
            return;
        }
        if ((data.dropped || []).length > 0) {
            // Non-blocking heads-up: open the tab AND show what was dropped.
            console.log('[scryfall] Dropped predicates:', data.dropped);
            // Briefly flash a tooltip-style note on the button. Bootstrap
            // tooltips would be nicer but a title-attribute swap is enough
            // for a one-off feedback.
            const oldTitle = btn.title;
            btn.title = 'Approximate — dropped: '
                + data.dropped.map(d => d.split('  —')[0]).join(', ');
            setTimeout(() => { btn.title = oldTitle; }, 6000);
        }
        window.open(data.url, '_blank', 'noopener');
    } catch (err) {
        console.error('[scryfall] translate failed', err);
        alert('Translation failed: ' + err.message);
    }
}

function _rebuildSortConfigTable(parsed) {
    const tbody = document.getElementById('sort-config-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    const binCount = parsed.bins;
    const fallback = parsed.fallback;

    for (let i = 1; i <= binCount; i++) {
        const query = parsed.binQueries[i] || '';
        const isOverride = parsed.overrides.has(i);
        const isFallback = (i === fallback);
        tbody.appendChild(_renderSortConfigRow(i, query, isOverride, isFallback));
    }

    document.getElementById('sc-bin-count').value = binCount;
    document.getElementById('sc-fallback').value = fallback;
    document.getElementById('sc-bin-limit').value = parsed.limit || '';
    document.getElementById('sort-config-description').textContent = parsed.description;
}

// ---------- Preset list ----------

async function loadSortConfigList() {
    const sel = document.getElementById('sort-preset-select');
    if (!sel) return;
    try {
        const data = await apiGet('/api/sort/configs');
        const configs = data.configs || [];
        // Refresh BUILTIN_CONFIGS and per-file metadata from the server so
        // the client doesn't drift from sort_configs/ on disk.
        if (Array.isArray(data.builtins)) {
            BUILTIN_CONFIGS = new Set(data.builtins);
        }
        _scPresetMeta = {};
        for (const cfg of configs) {
            _scPresetMeta[cfg.filename] = {
                description: cfg.description || '',
                bin_count: cfg.bin_count,
                builtin: !!cfg.builtin,
            };
        }
        sel.innerHTML = '';
        // Also populate the Dashboard mirror dropdown so the user can
        // pick the preset right next to the Start Session button without
        // having to navigate to the Sort Configuration tab.
        const dashSel = document.getElementById('dashboard-sort-preset-select');
        if (dashSel) dashSel.innerHTML = '';
        for (const cfg of configs) {
            const opt = document.createElement('option');
            opt.value = cfg.filename;
            const builtinTag = cfg.builtin ? ' (built-in)' : '';
            opt.textContent = cfg.filename + builtinTag;
            if (cfg.description) opt.title = cfg.description;
            sel.appendChild(opt);
            if (dashSel) {
                const opt2 = opt.cloneNode(true);
                dashSel.appendChild(opt2);
            }
        }
        if (configs.length) {
            // If current file is still in the list, keep selection; else first
            if (_scFilename && configs.some(c => c.filename === _scFilename)) {
                sel.value = _scFilename;
                if (dashSel) dashSel.value = _scFilename;
            } else {
                sel.value = configs[0].filename;
                if (dashSel) dashSel.value = configs[0].filename;
                await loadSortConfigFile(configs[0].filename);
            }
        }
    } catch (e) {
        addLog('Failed to load sort configs: ' + e);
    }
}

async function loadSortConfigFile(filename) {
    try {
        const data = await apiGet(`/api/sort/configs/${encodeURIComponent(filename)}`);
        const content = data.content || '';
        const parsed = _parseSortConfigText(content);
        _scFilename = filename;
        _scBuiltin = !!data.builtin || BUILTIN_CONFIGS.has(filename);
        _scDirty = false;
        _rebuildSortConfigTable(parsed);
        // Keep the raw textarea in sync with the loaded file — if the user
        // opens "Edit as text" later they see the real on-disk content
        // rather than whatever the table serializer would reconstruct.
        const ta = document.getElementById('sort-config-text-area');
        if (ta) ta.value = content;
        _updateSortConfigToolbar();
        // Sync BOTH preset selectors (Sort Config tab + Dashboard).
        const sel = document.getElementById('sort-preset-select');
        if (sel) sel.value = filename;
        const dashSel = document.getElementById('dashboard-sort-preset-select');
        if (dashSel) dashSel.value = filename;
    } catch (e) {
        addLog(`Failed to load sort config "${filename}": ` + e);
    }
}

function onSortPresetSelect() {
    const sel = document.getElementById('sort-preset-select');
    if (!sel || !sel.value) return;
    loadSortConfigFile(sel.value);
}

// Dashboard-side preset selector: same effect as the Sort Configuration
// tab's dropdown — load the file into the table so serializeSortConfig()
// picks up the right queries when Start Session fires.
function onDashboardSortPresetSelect() {
    const sel = document.getElementById('dashboard-sort-preset-select');
    if (!sel || !sel.value) return;
    loadSortConfigFile(sel.value);
}

// ---------- Dirty tracking ----------

function markSortConfigDirty() {
    _scDirty = true;
    const badge = document.getElementById('sort-config-dirty-badge');
    if (badge) badge.style.display = '';
    _updateSortConfigToolbar();
}

function _clearDirty() {
    _scDirty = false;
    const badge = document.getElementById('sort-config-dirty-badge');
    if (badge) badge.style.display = 'none';
    _updateSortConfigToolbar();
}

function _updateSortConfigToolbar() {
    const saveBtn = document.getElementById('btn-sort-config-save');
    const delBtn = document.getElementById('btn-sort-config-delete');
    const dupBtn = document.getElementById('btn-sort-config-duplicate');
    const editTextBtn = document.getElementById('btn-sort-config-edit-text');
    if (saveBtn) {
        saveBtn.disabled = _scBuiltin || !_scFilename;
        saveBtn.title = _scBuiltin
            ? 'Built-in presets are read-only; use Duplicate or Save As'
            : (_scFilename
                ? 'Save changes back to this preset file'
                : 'Pick a preset or Save As first');
    }
    if (delBtn) {
        delBtn.style.display = (!_scBuiltin && _scFilename) ? '' : 'none';
    }
    if (dupBtn) {
        // Can duplicate anything that's loaded on disk — builtin or user.
        dupBtn.disabled = !_scFilename;
    }
    if (editTextBtn) {
        editTextBtn.disabled = false;
    }
}

// ---------- Bin count change ----------

function onSortConfigBinCountChange() {
    const binCount = parseInt(document.getElementById('sc-bin-count').value) || 10;
    let fallback = parseInt(document.getElementById('sc-fallback').value) || binCount;
    if (fallback > binCount) {
        fallback = binCount;
        document.getElementById('sc-fallback').value = binCount;
    }
    const tbody = document.getElementById('sort-config-tbody');
    if (!tbody) return;

    // Preserve existing queries
    const existing = {};
    const existingOverrides = new Set();
    tbody.querySelectorAll('tr[data-bin]').forEach(tr => {
        const b = parseInt(tr.getAttribute('data-bin'));
        const inp = tr.querySelector('.sc-query-input');
        const cb = tr.querySelector('.sc-override-cb');
        if (inp) existing[b] = inp.value;
        if (cb && cb.checked) existingOverrides.add(b);
    });

    tbody.innerHTML = '';
    for (let i = 1; i <= binCount; i++) {
        const tr = _renderSortConfigRow(
            i,
            existing[i] || '',
            existingOverrides.has(i),
            i === fallback
        );
        tbody.appendChild(tr);
    }
    markSortConfigDirty();
}

// ---------- Override toggle ----------

function onOverrideToggle(binNum, cb) {
    const tr = cb.closest('tr');
    if (!tr) return;
    if (cb.checked) {
        tr.classList.add('is-override');
    } else {
        tr.classList.remove('is-override');
    }
    markSortConfigDirty();
}

// ---------- Add / remove rows ----------

function addSortConfigBin() {
    const tbody = document.getElementById('sort-config-tbody');
    if (!tbody) return;
    const rows = tbody.querySelectorAll('tr[data-bin]');
    const nextNum = rows.length + 1;
    // Update bin count field
    document.getElementById('sc-bin-count').value = nextNum;
    tbody.appendChild(_renderSortConfigRow(nextNum, '', false, false));
    markSortConfigDirty();
}

function removeSortConfigRow(btn) {
    const tr = btn.closest('tr');
    if (!tr) return;
    tr.remove();
    // Renumber
    const tbody = document.getElementById('sort-config-tbody');
    if (!tbody) return;
    let i = 1;
    const fallback = parseInt(document.getElementById('sc-fallback').value) || 0;
    tbody.querySelectorAll('tr[data-bin]').forEach(row => {
        row.setAttribute('data-bin', i);
        const binNumEl = row.querySelector('.bin-num');
        if (binNumEl) binNumEl.textContent = i;
        const overrideCb = row.querySelector('.sc-override-cb');
        if (overrideCb) overrideCb.setAttribute('onchange', `onOverrideToggle(${i}, this)`);
        const removeBtn = row.querySelector('.sc-remove-btn');
        if (removeBtn) removeBtn.setAttribute('onclick', `removeSortConfigRow(this)`);
        i++;
    });
    document.getElementById('sc-bin-count').value = i - 1;
    markSortConfigDirty();
}

// ---------- Per-row validation ----------

function _validateSortRow(input) {
    const query = input.value.trim();
    if (!query) {
        input.classList.remove('is-invalid');
        input.setCustomValidity('');
        return;
    }
    fetch('/api/sort/validate-query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }),
    })
    .then(r => r.json())
    .then(data => {
        if (data.valid) {
            input.classList.remove('is-invalid');
            input.setCustomValidity('');
        } else {
            input.classList.add('is-invalid');
            input.setCustomValidity(data.error || 'Invalid query');
            input.title = data.error || 'Invalid query';
        }
    })
    .catch(() => {});
}

// ---------- Pre-start validation ----------

async function _validateAllSortRows() {
    const tbody = document.getElementById('sort-config-tbody');
    if (!tbody) return true;
    const inputs = tbody.querySelectorAll('.sc-query-input:not(:disabled)');
    const checks = [];
    inputs.forEach(inp => {
        const query = inp.value.trim();
        if (!query) return;
        checks.push(
            fetch('/api/sort/validate-query', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query }),
            })
            .then(r => r.json())
            .then(data => ({ inp, valid: data.valid, error: data.error }))
        );
    });
    const results = await Promise.all(checks);
    let allValid = true;
    for (const r of results) {
        if (!r.valid) {
            r.inp.classList.add('is-invalid');
            r.inp.title = r.error || 'Invalid query';
            allValid = false;
        } else {
            r.inp.classList.remove('is-invalid');
        }
    }
    if (!allValid) {
        // Ensure the sort config panel is expanded so user can see the error
        const collapse = document.getElementById('sort-config-collapse');
        if (collapse && !collapse.classList.contains('show')) {
            new bootstrap.Collapse(collapse, { toggle: true });
        }
    }
    return allValid;
}

// ---------- New config ----------

function newSortConfig() {
    _scFilename = null;
    _scBuiltin = false;
    _scDirty = false;
    const empty = {
        description: '',
        bins: 10,
        fallback: 10,
        limit: null,
        overrides: new Set(),
        binQueries: {},
    };
    _rebuildSortConfigTable(empty);
    // Also reset the raw textarea so a stale previous preset doesn't
    // leak into the new one when the user opens "Edit as text".
    const ta = document.getElementById('sort-config-text-area');
    if (ta) ta.value = '# New preset\nbins: 10\nfallback: 10\n';
    _clearDirty();
    _updateSortConfigToolbar();
    const sel = document.getElementById('sort-preset-select');
    if (sel) sel.value = '';
}

// ---------- Save ----------

function _currentPresetContent() {
    // If the raw text editor is open, its textarea is authoritative —
    // otherwise serialize the bin table.
    const editor = document.getElementById('sort-config-text-editor');
    const ta = document.getElementById('sort-config-text-area');
    if (editor && ta && editor.classList.contains('show') && ta.value.trim()) {
        return ta.value;
    }
    return serializeSortConfig();
}

async function _savePresetToFile(filename, content) {
    const r = await fetch(
        `/api/sort/configs/${encodeURIComponent(filename)}`,
        {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
        }
    );
    if (r.ok) return { ok: true };
    let msg = `HTTP ${r.status}`;
    try {
        const body = await r.json();
        if (body.message) msg = body.message;
        else if (body.error) msg = body.error;
    } catch (e) {}
    return { ok: false, msg };
}

async function saveSortConfig() {
    if (!_scFilename || _scBuiltin) {
        addLog('Cannot overwrite a built-in preset. Use Save As.');
        return;
    }
    const content = _currentPresetContent();
    const res = await _savePresetToFile(_scFilename, content);
    if (res.ok) {
        _clearDirty();
        addLog(`Saved: ${_scFilename}`);
    } else {
        addLog(`Save failed: ${res.msg}`);
    }
}

async function saveSortConfigAs() {
    const suggested = _scFilename
        ? _scFilename.replace(/\.txt$/, '') + '_copy'
        : 'my_sort';
    const raw = prompt('Save as (filename without .txt):', suggested);
    if (!raw) return;
    const filename = raw.trim().replace(/\.txt$/, '') + '.txt';
    const content = _currentPresetContent();
    const res = await _savePresetToFile(filename, content);
    if (res.ok) {
        _scFilename = filename;
        _scBuiltin = false;
        _clearDirty();
        addLog(`Saved as: ${filename}`);
        await loadSortConfigList();
    } else {
        addLog(`Save failed: ${res.msg}`);
    }
}

async function deleteSortConfig() {
    if (!_scFilename || _scBuiltin) return;
    if (!confirm(`Delete "${_scFilename}"? This cannot be undone.`)) return;
    const r = await fetch(`/api/sort/configs/${encodeURIComponent(_scFilename)}`,
        { method: 'DELETE' });
    if (r.ok) {
        _scFilename = null;
        addLog('Deleted preset');
        await loadSortConfigList();
    } else {
        addLog(`Delete failed: ${r.status}`);
    }
}

// ---------- Duplicate ----------

async function duplicateSortConfig() {
    if (!_scFilename) {
        addLog('Nothing to duplicate — pick a preset first');
        return;
    }
    const base = _scFilename.replace(/\.txt$/, '');
    const raw = prompt(
        `Duplicate "${_scFilename}" as (filename without .txt):`,
        base + '_copy'
    );
    if (!raw) return;
    const target = raw.trim().replace(/\.txt$/, '') + '.txt';
    const r = await fetch(
        `/api/sort/configs/${encodeURIComponent(_scFilename)}/duplicate`,
        {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target }),
        }
    );
    if (r.ok) {
        const body = await r.json();
        addLog(`Duplicated to: ${body.filename}`);
        await loadSortConfigList();
        await loadSortConfigFile(body.filename);
    } else {
        let msg = `Duplicate failed: ${r.status}`;
        try {
            const body = await r.json();
            if (body.message) msg += ` — ${body.message}`;
            else if (body.error) msg += ` — ${body.error}`;
        } catch (e) {}
        addLog(msg);
    }
}

// ---------- Edit as text / textarea sync ----------

function onEditAsTextClicked() {
    // When the text editor is about to open, re-seed the textarea from
    // the current table state so the raw view matches what the user sees.
    // (If the user was editing the table, those changes get serialized in
    // here.) When the user later clicks away we re-parse back into rows.
    const ta = document.getElementById('sort-config-text-area');
    if (!ta) return;
    const editor = document.getElementById('sort-config-text-editor');
    // `collapse.show` is true *before* the toggle runs, so invert.
    const willOpen = editor && !editor.classList.contains('show');
    if (willOpen) {
        ta.value = serializeSortConfig();
    }
}

function _syncTextAreaToTable() {
    const ta = document.getElementById('sort-config-text-area');
    if (!ta) return;
    const text = ta.value;
    if (!text.trim()) return;  // nothing to parse
    const parsed = _parseSortConfigText(text);
    _rebuildSortConfigTable(parsed);
    // Validate every non-fallback query row we just rebuilt so the user
    // gets immediate feedback when leaving the textarea.
    if (typeof _validateAllSortRows === 'function') _validateAllSortRows();
}

// ---------- Session lock/unlock ----------

function _lockSortConfigPanel(locked) {
    const inputs = document.querySelectorAll(
        '#sort-config-collapse input, #sort-config-collapse select, #sort-config-collapse button'
    );
    inputs.forEach(el => { el.disabled = locked; });

    const hint = document.getElementById('sort-config-collapse-hint');
    if (hint) {
        hint.textContent = locked ? 'locked while session is active' : 'click to show/hide';
    }
}
