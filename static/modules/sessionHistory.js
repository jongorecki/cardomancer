// sessionHistory.js — Past sessions, DB info

async function loadSessionHistory() {
    try {
        const data = await apiGet('/api/collection/sessions');
        const tbody = document.getElementById('sessions-body');
        tbody.innerHTML = '';
        for (const s of data.sessions || []) {
            tbody.innerHTML += `<tr>
                <td>${s.id}</td>
                <td>${s.start_time || '?'}</td>
                <td>${s.sort_mode || '?'}</td>
                <td class="small">${s.notes || ''}</td>
                <td>${s.total_scans || 0}</td>
                <td>${s.recognized || 0}</td>
                <td>${s.unrecognized || 0}</td>
                <td><button class="btn btn-outline-danger btn-sm py-0 px-1" onclick="deleteSession(${s.id})" title="Delete session">&times;</button></td>
            </tr>`;
        }
    } catch (e) {}
}

// =========================================================================
// Database management
// =========================================================================

async function loadDbInfo() {
    try {
        const data = await apiGet('/api/database/status');
        const panel = document.getElementById('db-info');
        panel.innerHTML = `
            <p class="mb-1">Cards JSON: <strong>${data.cards_json || '?'}</strong> (${data.cards_json_size_mb || 0} MB)</p>
            <p class="mb-1">Card count: <strong>${data.card_count || 0}</strong></p>
            <p class="mb-1">Hash DB v1: ${data.hash_db_v1_exists ? `${data.hash_db_v1_count || '?'} entries (${data.hash_db_v1_size_mb || 0} MB)` : '<span class="text-danger">Not found</span>'}</p>
            <p class="mb-1">Hash DB v2: ${data.hash_db_v2_exists ? `${data.hash_db_v2_count || '?'} entries (${data.hash_db_v2_size_mb || 0} MB)` : '<span class="text-danger">Not found</span>'}</p>
            <p class="mb-0">Card images: <strong>${data.image_count || 0}</strong> (~${data.images_size_gb || 0} GB)</p>
        `;
    } catch (e) {}
}

async function checkDbUpdate() {
    const panel = document.getElementById('db-update-check');
    panel.textContent = 'Checking...';
    try {
        const data = await apiPost('/api/database/check-update');
        if (data.needs_update) {
            panel.innerHTML = `<span class="text-warning">Update available!</span><br>
                Current: ${data.current_file}<br>
                Available: ${data.remote_file}<br>
                Updated: ${data.updated_at}`;
        } else if (data.available) {
            panel.innerHTML = '<span class="text-success">Database is up to date.</span>';
        } else {
            panel.innerHTML = `<span class="text-danger">Error: ${data.error}</span>`;
        }
    } catch (e) {
        panel.textContent = 'Error checking for updates';
    }
}
