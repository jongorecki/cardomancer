// tour.js — First-run welcome tour
// =========================================================================
//
// Lightweight overlay-tooltip walkthrough for the Sort tab. Triggered
// either:
//   - from the home-getting-started banner ("Walk me through it" link),
//   - or programmatically the first time a user lands on Sort with no
//     saved sort presets.
//
// Five steps, all anchored to existing Sort-tab elements via querySelector.
// No external library — uses position: fixed overlays + arrow callouts so
// the asset cost stays at zero.
//
// State: persisted via localStorage('cm.tour.shown'). Operators can re-run
// the tour from the home-getting-started banner; setting that key to
// 'force' triggers it once on next load.

(function () {
    'use strict';

    const STORAGE_KEY = 'cm.tour.shown';
    let _overlay = null;
    let _stepIdx = 0;
    let _steps = [];

    function _shouldAutoStart() {
        try {
            return !localStorage.getItem(STORAGE_KEY);
        } catch (_) {
            return false;
        }
    }

    function _markShown() {
        try { localStorage.setItem(STORAGE_KEY, '1'); } catch (_) {}
    }

    // Public: start the tour. The Home tab's getting-started banner
    // and an autostart on first Sort tab open both call this.
    function startTour() {
        if (_overlay) return;
        _steps = _buildSteps();
        if (!_steps.length) return;
        _stepIdx = 0;
        _openOverlay();
        _renderStep();
    }

    // Public: dismiss + mark seen.
    function endTour() {
        _markShown();
        if (_overlay) {
            _overlay.remove();
            _overlay = null;
        }
    }

    function _openOverlay() {
        _overlay = document.createElement('div');
        _overlay.id = 'cm-tour-overlay';
        _overlay.className = 'cm-tour-overlay';
        // Click on the dim backdrop dismisses. Inner tooltip stops
        // propagation so accidental misses on the tooltip body don't
        // close the tour.
        _overlay.addEventListener('click', e => {
            if (e.target === _overlay) endTour();
        });
        document.body.appendChild(_overlay);
        // Esc dismisses too.
        document.addEventListener('keydown', _onKey);
    }

    function _onKey(e) {
        if (!_overlay) {
            document.removeEventListener('keydown', _onKey);
            return;
        }
        if (e.key === 'Escape') {
            endTour();
        } else if (e.key === 'ArrowRight' || e.key === 'Enter') {
            _nextStep();
        } else if (e.key === 'ArrowLeft') {
            _prevStep();
        }
    }

    function _nextStep() {
        if (_stepIdx >= _steps.length - 1) {
            endTour();
            return;
        }
        _stepIdx += 1;
        _renderStep();
    }

    function _prevStep() {
        if (_stepIdx <= 0) return;
        _stepIdx -= 1;
        _renderStep();
    }

    function _renderStep() {
        if (!_overlay) return;
        const step = _steps[_stepIdx];
        if (!step) {
            endTour();
            return;
        }
        // Switch to the right tab first so the target element is
        // actually visible.
        if (step.tab && typeof goToTab === 'function') {
            goToTab(step.tab);
        }

        // Render after a short delay so the tab show animation
        // settles before we measure the target's rect.
        setTimeout(() => {
            const target = step.targetSelector
                ? document.querySelector(step.targetSelector)
                : null;
            _overlay.innerHTML = `
                <div class="cm-tour-tip"
                     role="dialog"
                     aria-modal="true"
                     aria-labelledby="cm-tour-tip-title">
                    <div class="cm-tour-step">
                        Step ${_stepIdx + 1} of ${_steps.length}
                    </div>
                    <h2 id="cm-tour-tip-title" class="cm-tour-tip-title">
                        ${escapeHtml(step.title)}
                    </h2>
                    <p class="cm-tour-tip-body">${step.body}</p>
                    <div class="cm-tour-tip-actions">
                        <button type="button"
                                class="cm-tour-btn cm-tour-btn-skip"
                                onclick="endTour()">
                            Skip
                        </button>
                        <div class="cm-tour-nav">
                            <button type="button"
                                    class="cm-tour-btn cm-tour-btn-prev"
                                    ${_stepIdx === 0 ? 'disabled' : ''}
                                    onclick="_prevTourStep()">
                                Back
                            </button>
                            <button type="button"
                                    class="cm-tour-btn cm-tour-btn-next"
                                    onclick="_nextTourStep()">
                                ${_stepIdx >= _steps.length - 1 ? 'Finish' : 'Next'}
                            </button>
                        </div>
                    </div>
                </div>
            `;
            // If we have a target, position the tooltip near it AND
            // draw a highlight ring around the target. Otherwise the
            // tooltip centers on screen.
            const tip = _overlay.querySelector('.cm-tour-tip');
            if (target) {
                _drawHighlight(target);
                _positionTip(tip, target);
            } else {
                _clearClipHole();
                tip.style.position = 'fixed';
                tip.style.left = '50%';
                tip.style.top = '50%';
                tip.style.transform = 'translate(-50%, -50%)';
            }
        }, step.tab ? 240 : 0);
    }

    function _drawHighlight(target) {
        // Position a transparent box over the target with an amber
        // outline so the operator's eye lands on the right control.
        let ring = _overlay.querySelector('.cm-tour-highlight');
        if (!ring) {
            ring = document.createElement('div');
            ring.className = 'cm-tour-highlight';
            _overlay.appendChild(ring);
        }
        // Scroll the target into view so its rect is meaningful.
        try {
            target.scrollIntoView({ block: 'center', behavior: 'instant' });
        } catch (_) {
            target.scrollIntoView();
        }
        const r = target.getBoundingClientRect();
        const PAD = 6;
        ring.style.position = 'fixed';
        ring.style.left   = (r.left   - PAD) + 'px';
        ring.style.top    = (r.top    - PAD) + 'px';
        ring.style.width  = (r.width  + PAD * 2) + 'px';
        ring.style.height = (r.height + PAD * 2) + 'px';
        // Punch a transparent hole through the dim+blur overlay so
        // the highlighted control is sharp and readable. Without this
        // the operator sees a blurred version of the thing the tour
        // is asking them to look at, which defeats the point.
        _setClipHole(r, PAD);
    }

    // Carve the target rect out of the overlay using a CSS clip-path
    // polygon. The polygon traces the viewport (clockwise) and then
    // the inner rect (counter-clockwise); under the nonzero fill rule
    // the inner rect becomes a hole, so both the dim background AND
    // the backdrop-filter blur stop applying inside it.
    function _setClipHole(rect, pad) {
        if (!_overlay) return;
        const PAD = pad || 0;
        const w = window.innerWidth;
        const h = window.innerHeight;
        const x1 = Math.max(0, Math.floor(rect.left   - PAD));
        const y1 = Math.max(0, Math.floor(rect.top    - PAD));
        const x2 = Math.min(w, Math.ceil (rect.right  + PAD));
        const y2 = Math.min(h, Math.ceil (rect.bottom + PAD));
        _overlay.style.clipPath =
            'polygon(' +
                '0 0, 100% 0, 100% 100%, 0 100%, 0 0, ' +
                x1 + 'px ' + y1 + 'px, ' +
                x1 + 'px ' + y2 + 'px, ' +
                x2 + 'px ' + y2 + 'px, ' +
                x2 + 'px ' + y1 + 'px, ' +
                x1 + 'px ' + y1 + 'px' +
            ')';
        _overlay.style.webkitClipPath = _overlay.style.clipPath;
    }

    function _clearClipHole() {
        if (!_overlay) return;
        _overlay.style.clipPath = '';
        _overlay.style.webkitClipPath = '';
    }

    function _positionTip(tip, target) {
        // Place the tooltip in the largest free quadrant near the
        // target. For most Sort-tab controls, below is reliably
        // empty; fall back to above if below would clip.
        const r = target.getBoundingClientRect();
        const vp = { w: window.innerWidth, h: window.innerHeight };
        const TIP_W = 360;
        const TIP_GAP = 16;
        tip.style.position = 'fixed';
        tip.style.width = TIP_W + 'px';
        // Horizontal: align with target's left edge but clamp to viewport.
        let left = Math.max(16, Math.min(r.left, vp.w - TIP_W - 16));
        tip.style.left = left + 'px';
        // Vertical: prefer below; flip if below would clip the tip.
        const tipH_est = 220;  // approximate; final height set by content
        const below = r.bottom + TIP_GAP;
        if (below + tipH_est < vp.h) {
            tip.style.top = below + 'px';
        } else {
            tip.style.top = Math.max(16, r.top - tipH_est - TIP_GAP) + 'px';
        }
        tip.style.transform = 'none';
    }

    function _buildSteps() {
        // Each step: title, body, optional targetSelector, optional tab.
        // The selectors target stable Sort-tab elements; if any are
        // missing the step still renders (just without highlight).
        return [
            {
                tab: '#tab-sort',
                title: 'This is where you start a sort.',
                body: ('The big card here is the configurator. Each row '
                     + 'is one destination bin with a query that decides '
                     + 'which cards land there.'),
                targetSelector: '.cm-sort-hero-pre',
            },
            {
                tab: '#tab-sort',
                title: 'Pick a preset.',
                body: ('Cardomancer ships with built-in presets (color, '
                     + 'rarity, etc.) and you can save your own. Pick one '
                     + 'from the dropdown or build a custom config below.'),
                targetSelector: '#sort-preset-select',
            },
            {
                tab: '#tab-sort',
                title: 'Each bin gets a query.',
                body: ('Type a query like <code>c:r</code> for all red '
                     + 'cards, or <code>usd&gt;=1</code> for everything '
                     + 'worth a buck or more. Check the <strong>Override'
                     + '</strong> column to make a bin take priority '
                     + 'over the rest.'),
                targetSelector: '#sort-config-table',
            },
            {
                tab: '#tab-sort',
                title: 'Press Start when you’re ready.',
                body: ('The big green button kicks off the session. The '
                     + 'machine probes every bin, then starts picking '
                     + 'cards from the source and identifying each one '
                     + 'against the local Scryfall database.'),
                targetSelector: '.cm-sort-hero-pre .btn-success',
            },
            {
                tab: '#tab-home',
                title: 'You’re ready.',
                body: ('That’s the whole loop. The Cardomancer awaits '
                     + 'your stack.'),
                targetSelector: null,
            },
        ];
    }

    // Minimal escapeHtml (avoids loading util.js's version too early).
    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    // Expose hooks the buttons in the tip can call via onclick.
    window.startTour     = startTour;
    window.endTour       = endTour;
    window._nextTourStep = _nextStep;
    window._prevTourStep = _prevStep;

    // Auto-start on first Sort tab open if the operator has never
    // seen the tour. We wait for the Sort tab's `shown.bs.tab` so the
    // tour doesn't fire on initial page load (Home is the default tab).
    document.addEventListener('DOMContentLoaded', () => {
        const sortTab = document.querySelector('#mainTabs a[href="#tab-sort"]');
        if (!sortTab) return;
        sortTab.addEventListener('shown.bs.tab', function once() {
            sortTab.removeEventListener('shown.bs.tab', once);
            if (_shouldAutoStart()) {
                // Slight delay so the tab's own contents finish rendering.
                setTimeout(startTour, 350);
            }
        });
    });
})();
