// sortStage.js — Phase 4: staged Sort tab
//
// The Sort tab walks the user through three stages and only exposes
// controls relevant to the current one (per plans/sort_flow_stages.md):
//
//   pre      — configure bin queries / overflow / source. Big "Start".
//   running  — live camera, stats, bin tiles. Pause + Stop primary.
//   post     — session summary, review queue, "Start another" / "Edit".
//
// Sections in _tab_sort.html mark which stages they belong to via
//   data-stage="pre"          single-stage
//   data-stage="pre running"  multi-stage (space-separated)
//   data-stage="all"          always visible (header chrome etc.)
//
// CSS in style.css hides sections whose data-stage doesn't include the
// current stage. The active stage is the data-sort-stage attribute on
// the #tab-sort root.

let _currentStage = 'pre';

function _applyStage(stage) {
    _currentStage = stage;
    const root = document.getElementById('tab-sort');
    if (root) root.setAttribute('data-sort-stage', stage);
    if (typeof addLog === 'function') {
        addLog(`Sort stage: ${stage}`);
    }
}

function getSortStage() {
    return _currentStage;
}

// Public action: jump back to pre-sort. Used by the post-sort
// "Start another" / "Edit config" buttons. The "Start another"
// path expects the user to press Start again to launch; "Edit
// config" just gets them back to the configurator.
function returnToPreSort() {
    _applyStage('pre');
}

// Initial stage on page load. Trust the worker's reported state:
//   sorting / paused → user is mid-session, render running stage.
//   anything else    → no session, show the configurator.
//
// The DOMContentLoaded boot in app.js already polls /api/status and
// sets currentState; this function is called from there once the
// poll resolves so the data-sort-stage attribute is correct before
// any stage-tagged section renders.
function initSortStageFromState(workerState) {
    if (workerState === 'sorting' || workerState === 'paused') {
        _applyStage('running');
    } else {
        _applyStage('pre');
    }
}

// Stage transitions on session lifecycle events. Wired in socket.js:
//
//   session_started   → 'running'  (pre  → running)
//   session_ended     → 'post'     (running → post)
//   session_paused    → no change  (still running, just paused state)
//   sorter_state idle → 'post' if we were running, else stay
//
// The "session_resumed" frontend event doesn't exist as a backend
// emission; resume bumps state from 'paused' to 'sorting' which the
// existing sorter_state handler already updates. No stage change
// needed since paused is still a running stage.

function onSessionStarted() {
    _applyStage('running');
}

function onSessionEndedStageHook() {
    _applyStage('post');
}

// Power-loss / unclean-shutdown limbo: user hit a startup state where
// a session was active but didn't end cleanly. The Sort tab should
// surface a "Resume?" prompt rather than dropping straight into
// either pre-sort (loses the session) or running (resumes silently
// without confirmation). For Phase 4 part 5; stub the hook so other
// modules can call it now.
function onUnfinishedSessionDetected() {
    // Phase 4 part 5 — full Resume rehydration. For now we route to
    // post-sort so the user sees a summary; the stale-session banner
    // (already in place from roadblock #3) gives them the discard
    // affordance until the dedicated limbo state ships.
    _applyStage('post');
}
