// components.js — Reusable UI primitives for empty / loading / error states.
//
// Phase 3 polish: every panel that can be empty or loading now has a
// consistent place to render a thoughtful state instead of a bare
// "No data" or a blank panel. The design-system spec calls for:
//   - empty states: brand sigil + headline + supporting line + optional CTA
//   - skeleton loaders: subtle animated bars matching the panel shape
//
// Both pieces are pure DOM helpers — no framework, no state. Caller
// passes a target element (or its id) plus an options object; the
// helper writes innerHTML.

// Map of sigil name → public path. Add an entry here when a new sigil
// ships in static/branding/sigils/.
const _SIGIL_PATHS = {
    card:   '/static/branding/sigils/sigil-card.svg',
    eye:    '/static/branding/sigils/sigil-eye.svg',
    bin:    '/static/branding/sigils/sigil-bin.svg',
    stars:  '/static/branding/sigils/sigil-stars.svg',
    spiral: '/static/branding/sigils/sigil-spiral.svg',
};

function _resolveSigil(name) {
    if (!name) return null;
    return _SIGIL_PATHS[name] || null;
}

function _resolveContainer(target) {
    if (!target) return null;
    if (typeof target === 'string') return document.getElementById(target);
    return target;
}

/**
 * Render an empty-state into `target`.
 *
 * Options:
 *   title:      string  (required)        the headline
 *   body:       string  (optional)        the supporting line
 *   sigil:      'card' | 'eye' | 'bin' | 'stars' | 'spiral' | null
 *   sigilSize:  number  (default 48)      px size
 *   ctaLabel:   string  (optional)        text for the action button
 *   ctaOnClick: string  (optional)        inline JS to run on click
 *                                         (the helper writes onclick=...)
 *   variant:    'standard' | 'compact'    standard centers + pads more
 */
function renderEmptyState(target, opts) {
    const el = _resolveContainer(target);
    if (!el) return;
    const o = opts || {};
    const title = o.title || 'Nothing here yet.';
    const body = o.body || '';
    const sigilSrc = _resolveSigil(o.sigil);
    const sigilSize = o.sigilSize || 48;
    const ctaLabel = o.ctaLabel;
    const ctaOnClick = o.ctaOnClick || '';
    const variant = o.variant === 'compact' ? 'cm-empty-compact' : 'cm-empty-standard';

    const sigilHtml = sigilSrc
        ? `<img class="cm-empty-sigil" src="${sigilSrc}"
                alt="" width="${sigilSize}" height="${sigilSize}"
                aria-hidden="true">`
        : '';
    const bodyHtml = body
        ? `<p class="cm-empty-body small text-muted mb-0">${escapeHtml(body)}</p>`
        : '';
    const ctaHtml = ctaLabel
        ? `<button class="btn btn-sm btn-outline-secondary mt-3"
                   onclick="${ctaOnClick}">
              ${escapeHtml(ctaLabel)}
           </button>`
        : '';

    el.innerHTML = `
        <div class="cm-empty-state ${variant}">
            ${sigilHtml}
            <div class="cm-empty-title">${escapeHtml(title)}</div>
            ${bodyHtml}
            ${ctaHtml}
        </div>`;
}

/**
 * Render a skeleton loader into `target`. Use while a panel is
 * fetching data so the user sees motion + the right shape rather
 * than a blank or a loading-spinner-in-a-box.
 *
 * Options:
 *   rows:    number (default 3)       number of skeleton rows
 *   width:   string (default '100%')  outer width (CSS value)
 *   variant: 'lines' | 'card-row'     lines = full-width bars,
 *                                     card-row = a row of card-shaped tiles
 */
function renderSkeleton(target, opts) {
    const el = _resolveContainer(target);
    if (!el) return;
    const o = opts || {};
    const rows = Math.max(1, Math.min(20, o.rows || 3));
    const width = o.width || '100%';
    const variant = o.variant || 'lines';

    if (variant === 'card-row') {
        let html = `<div class="cm-skeleton-row" style="width:${width}">`;
        for (let i = 0; i < rows; i++) {
            html += '<div class="cm-skeleton-card"></div>';
        }
        html += '</div>';
        el.innerHTML = html;
        return;
    }

    // 'lines' variant — three line widths cycle so the skeleton
    // doesn't read as a perfect grid.
    let html = `<div class="cm-skeleton" style="width:${width}">`;
    const widths = ['85%', '70%', '92%'];
    for (let i = 0; i < rows; i++) {
        html += `<div class="cm-skeleton-line" style="width:${widths[i % widths.length]}"></div>`;
    }
    html += '</div>';
    el.innerHTML = html;
}

/**
 * Convenience: replace a panel's content with a skeleton, then call
 * an async loader. When the loader resolves, the caller renders the
 * real content (overwriting the skeleton). On rejection, the skeleton
 * is replaced with an empty-state that surfaces the error.
 */
async function withSkeleton(target, loader, skeletonOpts) {
    const el = _resolveContainer(target);
    if (!el) return null;
    renderSkeleton(el, skeletonOpts || {});
    try {
        return await loader();
    } catch (err) {
        renderEmptyState(el, {
            sigil: 'eye',
            title: 'Could not load this view.',
            body: (err && err.message) || String(err) || 'Try again in a moment.',
        });
        return null;
    }
}
