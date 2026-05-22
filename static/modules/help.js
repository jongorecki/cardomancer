// help.js — Initialize the cm-help-btn popovers
// =========================================================================
//
// The Jinja `help_btn` macro (templates/partials/_macros.html) emits
// <button class="cm-help-btn" data-bs-toggle="popover" ...> elements
// throughout the operator UI. Bootstrap popovers are JS-initialized;
// without this module they'd render as plain buttons that do nothing
// when clicked.
//
// We walk every `[data-cm-help-id]` on DOMContentLoaded and create a
// `bootstrap.Popover` for it. Re-runs cleanly if called twice (Bootstrap
// returns the existing instance via getOrCreateInstance).
//
// Telemetry hook: each button has a `data-cm-help-id` we can read in a
// click handler if we ever want to count which help topics get opened.
// Today: no telemetry, just init.

(function () {
    'use strict';

    function _init() {
        if (typeof bootstrap === 'undefined' || !bootstrap.Popover) {
            // Bootstrap not loaded yet — try again on the next animation
            // frame. Defensive: usually bootstrap is loaded before this
            // module, but the script tag order isn't guaranteed across
            // every entry point.
            requestAnimationFrame(_init);
            return;
        }
        document.querySelectorAll('[data-cm-help-id]').forEach(el => {
            bootstrap.Popover.getOrCreateInstance(el, {
                // Bootstrap default sanitizer strips most HTML. We
                // already authored the body content, so disable the
                // sanitizer (still escaping `title` because that
                // comes through textContent).
                sanitize: false,
                // Auto-flips between top/bottom/left/right based on
                // viewport space; honours data-bs-placement="auto"
                // from the macro.
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _init);
    } else {
        _init();
    }

    // Re-initialize when new content appears (e.g. tab switches that
    // render fresh markup, modals opening). We listen on the document
    // for Bootstrap's `shown.bs.tab` and `shown.bs.modal` events. Idempotent
    // because getOrCreateInstance returns the existing popover.
    document.addEventListener('shown.bs.tab',   _init);
    document.addEventListener('shown.bs.modal', _init);

    // Expose for tests + manual debug.
    window._initHelpPopovers = _init;
})();
