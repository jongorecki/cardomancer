// camera.js — Camera state, overlay, feed
// ---------------------------------------------------------------------------
// Camera health badge — polls /api/camera/status once per second so the user
// always has an at-a-glance indicator of whether the camera is alive, stalled,
// or dead. This is critical because everything the machine does depends on
// the camera; a silent freeze would be devastating without visibility.
// ---------------------------------------------------------------------------
let _cameraStatusLast = null;
async function pollCameraStatus() {
    try {
        const resp = await fetch('/api/camera/status');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const s = await resp.json();
        _cameraStatusLast = s;
        const badge = document.getElementById('camera-badge');
        if (!badge) return;
        const colorMap = {
            'ok': 'bg-success',
            'stalled': 'bg-warning text-dark',
            'dead': 'bg-danger',
            'unknown': 'bg-secondary',
        };
        const cls = colorMap[s.health] || 'bg-secondary';
        badge.className = 'badge ' + cls;
        let text = '\u{1F4F7} ' + (s.health || 'unknown');
        if (s.active && s.fps != null) {
            text += ' ' + s.fps.toFixed(0) + 'fps';
        }
        if (s.reconnects > 0) {
            text += ' ↻' + s.reconnects;
        }
        badge.textContent = text;
        badge.title = 'Camera: ' + (s.health || 'unknown')
            + ' | fps=' + (s.fps != null ? s.fps : '?')
            + ' | frames=' + (s.frame_count != null ? s.frame_count : '?')
            + ' | reconnects=' + (s.reconnects || 0)
            + ' | read_failures=' + (s.read_failures || 0)
            + ' | since_frame=' + (s.seconds_since_frame != null ? s.seconds_since_frame + 's' : '?')
            + (s.last_error ? '\nlast_error: ' + s.last_error : '')
            + '\nClick for details';
    } catch (err) {
        const badge = document.getElementById('camera-badge');
        if (badge) {
            badge.className = 'badge bg-danger';
            badge.textContent = '\u{1F4F7} ERR';
            badge.title = 'Camera status fetch failed: ' + err;
        }
    }
}
function showCameraDetails() {
    const s = _cameraStatusLast;
    if (!s) {
        alert('No camera status available yet.');
        return;
    }
    const lines = [
        'Camera Status',
        '─────────────────',
        'active:            ' + s.active,
        'device_index:      ' + s.device_index,
        'health:            ' + s.health,
        'fps:               ' + s.fps,
        'frame_count:       ' + s.frame_count,
        'reconnects:        ' + s.reconnects,
        'read_failures:     ' + s.read_failures,
        'seconds_since_frame: ' + s.seconds_since_frame,
        'last_error:        ' + (s.last_error || '(none)'),
    ];
    alert(lines.join('\n'));
}
// Start polling immediately and every second thereafter.
setInterval(pollCameraStatus, 1000);
pollCameraStatus();

// =========================================================================
// Camera Feed Overlay
// =========================================================================

let _overlayTimeout = null;

function drawCameraOverlay(data) {
    const canvas = document.getElementById('camera-overlay-canvas');
    const img = document.getElementById('session-camera-feed');
    if (!canvas || !img) return;

    // Match canvas size to displayed image
    const rect = img.getBoundingClientRect();
    canvas.width = rect.width;
    canvas.height = rect.height;

    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    if (!data) return;

    // Semi-transparent banner at top
    const bannerH = 60;
    ctx.fillStyle = data.recognized ? 'rgba(0, 40, 0, 0.75)' : 'rgba(80, 0, 0, 0.75)';
    ctx.fillRect(0, 0, canvas.width, bannerH);

    // Card name
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 18px sans-serif';
    ctx.textBaseline = 'top';
    const name = data.recognized ? data.name : 'UNRECOGNIZED';
    ctx.fillText(name, 10, 8);

    // Details line
    ctx.font = '13px sans-serif';
    ctx.fillStyle = '#ccc';
    if (data.recognized) {
        const borderTag = data.border === 'borderless' ? ' | BL' : '';
        const details = `${data.set || ''} | ${(data.colors || []).join('')} | Bin ${data.bin} | ${data.method || ''}${borderTag}`;
        ctx.fillText(details, 10, 32);
    } else {
        ctx.fillText(`-> Bin ${data.bin || '?'}`, 10, 32);
    }

    // Price badge (bottom-right)
    if (data.price && data.price !== 'N/A') {
        const priceStr = data.price;
        ctx.font = 'bold 16px sans-serif';
        const pw = ctx.measureText(priceStr).width + 16;
        ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
        ctx.fillRect(canvas.width - pw - 8, canvas.height - 36, pw, 28);
        ctx.fillStyle = '#4f4';
        ctx.fillText(priceStr, canvas.width - pw - 1, canvas.height - 32);
    }

    // Auto-fade after 5 seconds
    clearTimeout(_overlayTimeout);
    _overlayTimeout = setTimeout(() => {
        const c = document.getElementById('camera-overlay-canvas');
        if (c) c.getContext('2d').clearRect(0, 0, c.width, c.height);
    }, 5000);
}

// =========================================================================
// Live Card Info panel (Phase 4 item 4.19)
// =========================================================================
//
// Shows image, name, set, border, and TCGPlayer USD price for the card
// the worker just identified. Subscribes to the same `card_detected`
// socket event as the other handlers, then fetches
// /api/card/overlay-info?name=...&set=... for the image + border fields
// that the worker payload does not include.
//
// v1 deliberately shows only: image, name, set, border, price. Tags,
// salt, combos, and deck-usage are deferred to a future drawer.

let _cardOverlayReqId = 0;

function _borderBadgeClass(border) {
    switch ((border || '').toLowerCase()) {
        case 'black':      return 'bg-dark text-light';
        case 'white':      return 'bg-light text-dark border';
        case 'silver':     return 'bg-secondary';
        case 'gold':       return 'bg-warning text-dark';
        case 'borderless': return 'bg-info text-dark';
        default:           return 'bg-secondary';
    }
}

function renderCardOverlay(info, fallbackPrice) {
    const panel = document.getElementById('card-overlay-panel');
    if (!panel) return;
    if (!info) {
        panel.innerHTML = '<p class="text-muted small mb-0">Card not found in local data.</p>';
        return;
    }
    const name = info.name || '(unknown)';
    const setCode = (info.set || '').toUpperCase();
    const cn = info.collector_number ? ` #${info.collector_number}` : '';
    const border = info.border || 'unknown';
    const badgeCls = _borderBadgeClass(border);
    // Price: prefer server-side float; fall back to the string the worker
    // already put on the card_detected event.
    let priceText = 'N/A';
    if (typeof info.price_usd === 'number' && !isNaN(info.price_usd)) {
        priceText = '$' + info.price_usd.toFixed(2);
    } else if (fallbackPrice && fallbackPrice !== 'N/A') {
        priceText = fallbackPrice;
    }
    const imgHtml = info.image_url
        ? `<img src="${info.image_url}" alt="${name}"
               style="width:100%; max-width:240px; border-radius:4.75% / 3.5%;
                      display:block; margin:0 auto 0.5rem;">`
        : `<div class="text-muted small text-center mb-2"
               style="height:160px; display:flex; align-items:center;
                      justify-content:center; border:1px dashed #555;">
             (no image available)
           </div>`;
    panel.innerHTML = `
        ${imgHtml}
        <div class="d-flex justify-content-between align-items-baseline mb-1">
            <strong style="word-break:break-word;">${name}</strong>
            <span class="small text-muted ms-2">${setCode}${cn}</span>
        </div>
        <div class="d-flex justify-content-between align-items-center">
            <span class="badge ${badgeCls}" title="Border color">${border}</span>
            <span class="small"><strong>${priceText}</strong></span>
        </div>
    `;
}

// =========================================================================
// Camera feed helpers
// =========================================================================

const _feedIntervals = {};

function startCameraFeed(imgId) {
    const img = document.getElementById(imgId);
    if (!img) return;
    // Start camera on backend
    apiPost('/api/camera/start');
    // Set MJPEG source
    img.src = '/api/camera/feed?' + Date.now();
    img.style.display = '';
}

function stopCameraFeed(imgId) {
    const img = document.getElementById(imgId);
    if (!img) return;
    img.src = '';
}
