// wishlist.js — Wishlist
// =========================================================================
// Wishlist
// =========================================================================

async function loadWishlist() {
    try {
        const data = await apiGet('/api/collection/wishlist');
        const tbody = document.getElementById('wishlist-body');
        const items = data.items || [];
        document.getElementById('wishlist-count').textContent = items.filter(i => !i.found).length;
        tbody.innerHTML = '';
        for (const w of items) {
            const priceStr = w.max_price ? `$${w.max_price.toFixed(2)}` : '';
            const foundClass = w.found ? 'text-decoration-line-through text-muted' : '';
            const priorityBadge = w.priority === 'high'
                ? '<span class="badge bg-danger">High</span>'
                : w.priority === 'low'
                    ? '<span class="badge bg-secondary">Low</span>'
                    : '<span class="badge bg-info">Normal</span>';
            tbody.innerHTML += `<tr class="${foundClass}">
                <td>${w.name}</td>
                <td class="small">${w.set_code || ''}</td>
                <td>${priceStr}</td>
                <td>${priorityBadge}</td>
                <td class="small">${w.notes || ''}</td>
                <td class="text-nowrap">
                    ${!w.found ? `<button class="btn btn-outline-success btn-sm py-0 px-1" onclick="markWishlistFound(${w.id})" title="Mark found">&#10003;</button>` : ''}
                    <button class="btn btn-outline-danger btn-sm py-0 px-1" onclick="deleteWishlistItem(${w.id})" title="Remove">&times;</button>
                </td>
            </tr>`;
        }
    } catch (e) {}
}

async function addWishlistItem() {
    const name = document.getElementById('wish-name').value.trim();
    if (!name) { alert('Card name is required'); return; }
    await apiPost('/api/collection/wishlist', {
        name,
        set_code: document.getElementById('wish-set').value.trim(),
        max_price: parseFloat(document.getElementById('wish-price').value) || null,
        priority: document.getElementById('wish-priority').value,
        notes: document.getElementById('wish-notes').value.trim(),
    });
    document.getElementById('wish-name').value = '';
    document.getElementById('wish-set').value = '';
    document.getElementById('wish-price').value = '';
    document.getElementById('wish-notes').value = '';
    loadWishlist();
}

async function deleteWishlistItem(id) {
    await fetch(`/api/collection/wishlist/${id}`, { method: 'DELETE' });
    loadWishlist();
}

async function markWishlistFound(id) {
    await apiPost(`/api/collection/wishlist/${id}/found`);
    loadWishlist();
}
