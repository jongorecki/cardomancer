// moxfield.js — Moxfield text Import / Export
// =========================================================================
// Moxfield text Import / Export
// =========================================================================

async function submitMoxfieldPaste() {
    const srcKey = (document.getElementById('mox-paste-source-key').value || '').trim();
    const display = (document.getElementById('mox-paste-display-name').value || '').trim();
    const text = document.getElementById('mox-paste-textarea').value || '';
    const result = document.getElementById('mox-paste-result');

    if (!srcKey) {
        result.innerHTML = '<span class="text-danger">Source key is required (e.g. <code>moxfield:my-edh</code>).</span>';
        return;
    }
    if (!text.trim()) {
        result.innerHTML = '<span class="text-danger">Paste some decklist text first.</span>';
        return;
    }

    result.innerHTML = '<span class="text-muted">Importing…</span>';
    try {
        const resp = await fetch('/api/integrations/moxfield/wishlist/paste', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_key: srcKey,
                display_name: display || null,
                text,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            result.innerHTML = `<span class="text-danger">Error: ${data.error || resp.status}</span>`;
            return;
        }
        let msg = `<span class="text-success">Imported ${data.card_count} card${data.card_count === 1 ? '' : 's'} into <code>${data.source_key}</code>.</span>`;
        if ((data.unresolved || []).length) {
            msg += `<details class="mt-2"><summary>${data.unresolved.length} unresolved name${data.unresolved.length === 1 ? '' : 's'}</summary>`
                 + `<ul class="small mb-0">${data.unresolved.map(n => `<li>${escapeHtml(n)}</li>`).join('')}</ul></details>`;
        }
        result.innerHTML = msg;
        loadPriorityWishlistSources();
    } catch (e) {
        result.innerHTML = `<span class="text-danger">Request failed: ${e.message || e}</span>`;
    }
}

async function loadMoxfieldExportText() {
    const ta = document.getElementById('mox-export-textarea');
    const copyBtn = document.getElementById('mox-export-copy-btn');
    if (!ta) return;
    ta.value = 'Loading…';
    copyBtn.style.display = 'none';
    try {
        const resp = await fetch('/api/integrations/moxfield/export-text');
        ta.value = await resp.text();
        copyBtn.style.display = '';
    } catch (e) {
        ta.value = `Error: ${e.message || e}`;
    }
}

async function copyMoxfieldExportText() {
    const ta = document.getElementById('mox-export-textarea');
    if (!ta || !ta.value) return;
    try {
        await navigator.clipboard.writeText(ta.value);
        const btn = document.getElementById('mox-export-copy-btn');
        const orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => { btn.textContent = orig; }, 1500);
    } catch {
        ta.select();
        document.execCommand('copy');
    }
}
