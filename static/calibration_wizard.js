// calibration_wizard.js
// ---------------------------------------------------------------------------
// Phase 4 item 4.22 — Calibration Wizard.
//
// Drives a 7-step state machine that orchestrates (does NOT reimplement)
// existing calibration endpoints. Goal: after the wizard runs once, a
// fresh sort session is a single "Start" click.
//
// Steps (per user spec 2026-04-23):
//   1. Connect to the machine         -> POST /api/connect
//   2. Connect to the camera          -> POST /api/camera/start
//   3. Confirm ArUco markers in place -> pure UI confirmation
//   4. Run ArUco bin X calibration    -> POST /api/calibration/start
//   5. Probe the bin Z positions      -> POST /api/bins/probe-all
//   6. Set the staging platform ROI   -> POST /api/session/set-staging-roi
//   7. Confirm + lock camera focus    -> POST /api/calibration/wizard/lock-focus
//
// Status pre-check:
//   GET /api/calibration/status -> { wizard: { ...per-step booleans... } }
// The wizard auto-skips every step that's already satisfied on open.
// ---------------------------------------------------------------------------

(function () {
    'use strict';

    // Total step count — keep in sync with the rail/spec.
    const TOTAL_STEPS = 7;

    // Canonical step metadata. `statusKey` maps to a boolean in the
    // /api/calibration/status `wizard` payload. `run()` performs the
    // step's action and should resolve to {ok: true} on success.
    const STEPS = [
        {
            id: 1,
            title: 'Connect to the machine',
            statusKey: 'machine_connected',
            describe: () =>
                'Open the serial port to the motion controller so the wizard ' +
                'can issue G-code. Uses the same endpoint as the Dashboard ' +
                '"Connect" button.',
            run: async () => {
                await apiPost('/api/connect');
                return waitForStatus('machine_connected', 10000);
            },
        },
        {
            id: 2,
            title: 'Connect to the camera',
            statusKey: 'camera_connected',
            describe: () =>
                'Start the camera device so subsequent steps (ArUco sweep, ' +
                'ROI drawing, focus lock) have a live feed. Uses ' +
                '/api/camera/start.',
            run: async () => {
                await apiPost('/api/camera/start');
                return waitForStatus('camera_connected', 8000);
            },
        },
        {
            id: 3,
            title: 'Confirm ArUco markers are in place',
            statusKey: null, // no on-disk signal; always shown at least once per session
            skipIf: (state) => state._arucoConfirmed === true,
            describe: () =>
                'Confirm that each bin has its ArUco marker installed (source ' +
                'IDs 0–9, destinations 10–48, staging 49). Click the button ' +
                'below when the markers are physically in place.',
            render: (body) => {
                body.innerHTML = `
                    <div class="text-secondary small">
                        Expected layout (left to right on the X rail):
                        <ul class="mb-2">
                            <li>Source bin(s): marker IDs 0–9</li>
                            <li>Staging platform: marker ID 49</li>
                            <li>Destination bins: marker IDs 10–48</li>
                        </ul>
                        Markers must sit flat at the bottom of each bin,
                        fully visible from the camera over the X carriage.
                    </div>
                    <div class="text-center my-2">
                        <img src="/api/camera/feed-aruco" alt="Live ArUco overlay"
                             class="img-fluid rounded border border-secondary"
                             style="max-height:280px;background:#111">
                    </div>
                    <p class="small text-muted">
                        Live overlay shown for sanity. Actual detection runs
                        in the next step.
                    </p>`;
            },
            nextLabel: 'Markers are in place',
            run: async (state) => {
                state._arucoConfirmed = true;
                return { ok: true };
            },
        },
        {
            id: 4,
            title: 'Run ArUco bin X calibration',
            statusKey: 'bin_x_calibrated',
            describe: () =>
                'Sweep the X axis and record each bin\'s X position by ' +
                'centering on its ArUco marker. Uses /api/calibration/start ' +
                'with the current settings on the Calibration tab.',
            render: (body) => {
                body.innerHTML = `
                    <div class="row g-2 mb-2">
                        <div class="col-4">
                            <label class="form-label small">Source bins</label>
                            <input type="number" id="wiz-expected-sources"
                                   class="form-control form-control-sm"
                                   value="1" min="1" max="5">
                        </div>
                        <div class="col-4">
                            <label class="form-label small">Destination bins</label>
                            <input type="number" id="wiz-expected-dests"
                                   class="form-control form-control-sm"
                                   value="7" min="1" max="40">
                        </div>
                        <div class="col-4">
                            <label class="form-label small">Max sweep X (mm)</label>
                            <input type="number" id="wiz-max-sweep-x"
                                   class="form-control form-control-sm"
                                   value="790" min="100" max="2000">
                        </div>
                    </div>
                    <div class="form-check mb-2">
                        <input class="form-check-input" type="checkbox"
                               id="wiz-expect-staging" checked>
                        <label class="form-check-label small" for="wiz-expect-staging">
                            Expect staging platform marker (ID 49)
                        </label>
                    </div>
                    <div id="wiz-sweep-progress" class="small text-secondary"></div>`;
            },
            run: async () => {
                const payload = {
                    expected_sources: parseInt(
                        document.getElementById('wiz-expected-sources').value, 10) || 1,
                    expected_dests: parseInt(
                        document.getElementById('wiz-expected-dests').value, 10) || 7,
                    expect_staging: document.getElementById('wiz-expect-staging').checked,
                    max_sweep_x: parseFloat(
                        document.getElementById('wiz-max-sweep-x').value) || 790,
                };
                const resp = await apiPost('/api/calibration/start', payload);
                if (!resp || !resp.ok) {
                    return { ok: false, message: resp && resp.message || 'sweep rejected' };
                }
                return waitForStatus('bin_x_calibrated', 180000, {
                    progressEl: document.getElementById('wiz-sweep-progress'),
                    progressText: 'Sweeping… this can take a minute.',
                });
            },
        },
        {
            id: 5,
            title: 'Probe bin Z positions',
            statusKey: 'bin_z_probed',
            describe: () =>
                'Lower the suction head into each bin to record its Z depth ' +
                '(so the machine knows how far to go to pick / drop). Uses ' +
                '/api/bins/probe-all.',
            run: async () => {
                const resp = await apiPost('/api/bins/probe-all');
                if (!resp || !resp.ok) {
                    return { ok: false, message: resp && resp.message || 'probe rejected' };
                }
                return waitForStatus('bin_z_probed', 240000, {
                    progressText: 'Probing every bin… watch the machine.',
                });
            },
        },
        {
            id: 6,
            title: 'Set staging platform ROI',
            statusKey: 'staging_roi_set',
            describe: () =>
                'Click the 4 corners of the staging platform on the live ' +
                'camera feed. The detector crops to this quad when finding ' +
                'cards. Saved to staging_roi.json.',
            render: (body) => {
                body.innerHTML = `
                    <p id="wiz-roi-status" class="small mb-1" style="color:var(--accent-yellow)">
                        Click corner 1 of 4 (Top-Left)
                    </p>
                    <div style="position:relative;display:inline-block;max-width:100%;
                                max-height:55vh;cursor:crosshair;">
                        <img id="wiz-roi-img" src="/api/camera/feed"
                             style="max-width:100%;max-height:55vh;display:block;border-radius:6px;">
                        <canvas id="wiz-roi-canvas"
                                style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:auto;">
                        </canvas>
                    </div>
                    <div class="mt-2 d-flex gap-2">
                        <button type="button" id="wiz-roi-reset"
                                class="btn btn-sm btn-outline-secondary">Reset</button>
                    </div>`;
                initRoiPicker();
            },
            // Next only enabled when the user has placed 4 corners.
            nextDisabledUntil: (state) =>
                !state._roiCorners || state._roiCorners.length !== 4,
            run: async (state) => {
                if (!state._roiCorners || state._roiCorners.length !== 4) {
                    return { ok: false, message: 'Place 4 corners first.' };
                }
                const resp = await apiPost('/api/session/set-staging-roi',
                                            { corners: state._roiCorners });
                if (!resp || !resp.ok) {
                    return { ok: false, message: resp && resp.message || 'save failed' };
                }
                // staging_roi.json is written synchronously by the endpoint
                // via worker.set_staging_roi -> save_staging_roi(). No need
                // to poll — but we still hit status once so the next-tick
                // summary is correct.
                return waitForStatus('staging_roi_set', 5000);
            },
        },
        {
            id: 7,
            title: 'Confirm focus and lock',
            statusKey: 'focus_locked',
            describe: () =>
                'Check the live feed is sharp, then lock the autofocus. ' +
                'The camera freezes at its current focal distance so the ' +
                'rest of the session doesn\'t re-hunt between cards.',
            render: (body) => {
                body.innerHTML = `
                    <div class="text-center my-2">
                        <img src="/api/camera/feed"
                             alt="Focus preview"
                             class="img-fluid rounded border border-secondary"
                             style="max-height:55vh;background:#111">
                    </div>
                    <p class="small text-muted">
                        If the feed is soft, slide a card onto the staging
                        platform and wait a moment for autofocus to settle
                        before hitting "Lock focus".
                    </p>`;
            },
            nextLabel: 'Lock focus',
            run: async () => {
                const resp = await apiPost('/api/calibration/wizard/lock-focus');
                if (!resp || !resp.ok) {
                    return { ok: false, message: resp && resp.message || 'lock failed' };
                }
                return waitForStatus('focus_locked', 5000);
            },
        },
    ];

    const state = {
        currentStep: 1,
        satisfied: {},       // { statusKey: bool } snapshot from last /status fetch
        _arucoConfirmed: false,
        _roiCorners: null,
    };

    // ----------------------------------------------------------------------
    // Pre-check + step navigation
    // ----------------------------------------------------------------------

    async function fetchStatus() {
        try {
            const resp = await fetch('/api/calibration/status', { cache: 'no-store' });
            if (!resp.ok) return null;
            const body = await resp.json();
            return body.wizard || null;
        } catch (_) {
            return null;
        }
    }

    // Poll /api/calibration/status until `key` is true, or until `timeoutMs`.
    // Returns {ok:true} on success, {ok:false, message:'timeout'} otherwise.
    async function waitForStatus(key, timeoutMs, opts) {
        opts = opts || {};
        const started = Date.now();
        if (opts.progressEl && opts.progressText) {
            opts.progressEl.textContent = opts.progressText;
        }
        while (Date.now() - started < timeoutMs) {
            const s = await fetchStatus();
            if (s && s[key]) {
                state.satisfied = Object.assign(state.satisfied, s);
                return { ok: true };
            }
            await sleep(800);
        }
        return { ok: false, message: `Timed out waiting for ${key}` };
    }

    function sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    function stepByIndex(i) {
        return STEPS.find((s) => s.id === i);
    }

    function isStepSatisfied(step) {
        if (step.skipIf && step.skipIf(state)) return true;
        if (!step.statusKey) return false;
        return !!state.satisfied[step.statusKey];
    }

    function nextUnsatisfiedFrom(startId) {
        for (let i = startId; i <= TOTAL_STEPS; i++) {
            const s = stepByIndex(i);
            if (!isStepSatisfied(s)) return i;
        }
        return TOTAL_STEPS + 1; // all done
    }

    // ----------------------------------------------------------------------
    // Rendering
    // ----------------------------------------------------------------------

    function renderRail() {
        document.querySelectorAll('#wizard-step-rail .wizard-step-pill')
            .forEach((el) => {
                const id = parseInt(el.getAttribute('data-wizard-step'), 10);
                el.classList.toggle('is-active', id === state.currentStep);
                const step = stepByIndex(id);
                el.classList.toggle('is-done', step ? isStepSatisfied(step) : false);
            });
    }

    function renderHeader() {
        const hdr = document.getElementById('wizard-step-header');
        if (state.currentStep > TOTAL_STEPS) {
            hdr.textContent = 'Complete';
            hdr.className = 'badge bg-success ms-2';
        } else {
            hdr.textContent = `Step ${state.currentStep} of ${TOTAL_STEPS}`;
            hdr.className = 'badge bg-secondary ms-2';
        }
    }

    function renderStep() {
        clearNotices();
        renderHeader();
        renderRail();
        const body = document.getElementById('wizard-step-body');
        const title = document.getElementById('wizard-step-title');
        const nextBtn = document.getElementById('wizard-btn-next');
        const backBtn = document.getElementById('wizard-btn-back');
        const skipBtn = document.getElementById('wizard-btn-skip');

        backBtn.disabled = state.currentStep <= 1;

        if (state.currentStep > TOTAL_STEPS) {
            title.textContent = 'All steps complete';
            body.innerHTML =
                '<p>The machine is calibrated. Close this window, pick ' +
                'your sort criteria on the Sort Session tab, and hit Start.</p>';
            nextBtn.textContent = 'Done';
            nextBtn.disabled = false;
            skipBtn.disabled = true;
            return;
        }

        const step = stepByIndex(state.currentStep);
        title.textContent = step.title;
        if (step.render) {
            step.render(body);
        } else {
            body.innerHTML = `<p class="text-secondary">${escapeHtml(step.describe())}</p>`;
        }
        nextBtn.textContent = step.nextLabel || 'Next';
        nextBtn.disabled = false;
        skipBtn.disabled = false;

        // Some steps require user input before Next is valid.
        if (step.nextDisabledUntil && step.nextDisabledUntil(state)) {
            nextBtn.disabled = true;
        }
    }

    function setError(msg) {
        const el = document.getElementById('wizard-error');
        el.textContent = msg;
        el.style.display = '';
    }
    function setInfo(msg) {
        const el = document.getElementById('wizard-info');
        el.textContent = msg;
        el.style.display = '';
    }
    function clearNotices() {
        document.getElementById('wizard-error').style.display = 'none';
        document.getElementById('wizard-info').style.display = 'none';
    }

    function escapeHtml(s) {
        const d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
    }

    // ----------------------------------------------------------------------
    // Staging ROI picker (step 6)
    // ----------------------------------------------------------------------

    function initRoiPicker() {
        state._roiCorners = [];
        const img = document.getElementById('wiz-roi-img');
        const canvas = document.getElementById('wiz-roi-canvas');
        const statusEl = document.getElementById('wiz-roi-status');
        const ctx = canvas.getContext('2d');
        const labels = ['Top-Left', 'Top-Right', 'Bottom-Right', 'Bottom-Left'];
        // Use naturalWidth/Height once loaded for pixel-space conversion.
        // MJPEG streams fire onload on every frame — cheap to re-sync.

        function sync() {
            canvas.width = img.clientWidth;
            canvas.height = img.clientHeight;
        }

        function redraw() {
            sync();
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            if (state._roiCorners.length === 0) return;
            const imgW = img.naturalWidth || img.clientWidth || 1080;
            const imgH = img.naturalHeight || img.clientHeight || 1920;
            const sx = canvas.width / imgW;
            const sy = canvas.height / imgH;
            ctx.strokeStyle = '#4caf7c';
            ctx.lineWidth = 2;
            ctx.beginPath();
            state._roiCorners.forEach((p, i) => {
                const x = p[0] * sx, y = p[1] * sy;
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            if (state._roiCorners.length === 4) {
                ctx.lineTo(state._roiCorners[0][0] * sx,
                           state._roiCorners[0][1] * sy);
            }
            ctx.stroke();
            state._roiCorners.forEach((p, i) => {
                const x = p[0] * sx, y = p[1] * sy;
                ctx.fillStyle = '#4caf7c';
                ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fill();
                ctx.fillStyle = '#fff'; ctx.font = '12px sans-serif';
                ctx.fillText(labels[i], x + 10, y - 8);
            });
        }

        img.onload = () => { sync(); redraw(); };

        canvas.addEventListener('click', (e) => {
            if (state._roiCorners.length >= 4) return;
            const rect = canvas.getBoundingClientRect();
            const imgW = img.naturalWidth || img.clientWidth || 1080;
            const imgH = img.naturalHeight || img.clientHeight || 1920;
            const px = Math.round((e.clientX - rect.left) / canvas.width * imgW);
            const py = Math.round((e.clientY - rect.top) / canvas.height * imgH);
            state._roiCorners.push([px, py]);
            redraw();
            if (state._roiCorners.length < 4) {
                statusEl.textContent =
                    `Click corner ${state._roiCorners.length + 1} of 4 ` +
                    `(${labels[state._roiCorners.length]})`;
            } else {
                statusEl.textContent = 'All 4 corners set — hit "Next" to save.';
                statusEl.style.color = 'var(--accent-green)';
                document.getElementById('wizard-btn-next').disabled = false;
            }
        });

        document.getElementById('wiz-roi-reset').addEventListener('click', () => {
            state._roiCorners = [];
            redraw();
            statusEl.textContent = 'Click corner 1 of 4 (Top-Left)';
            statusEl.style.color = 'var(--accent-yellow)';
            document.getElementById('wizard-btn-next').disabled = true;
        });
    }

    // ----------------------------------------------------------------------
    // Control handlers
    // ----------------------------------------------------------------------

    async function onNext() {
        clearNotices();
        const step = stepByIndex(state.currentStep);
        if (state.currentStep > TOTAL_STEPS) {
            // "Done" — close the modal.
            const modalEl = document.getElementById('calibration-wizard-modal');
            const inst = bootstrap.Modal.getInstance(modalEl);
            if (inst) inst.hide();
            return;
        }
        const nextBtn = document.getElementById('wizard-btn-next');
        nextBtn.disabled = true;
        const original = nextBtn.textContent;
        nextBtn.textContent = 'Working…';
        try {
            const result = await step.run(state);
            if (!result || !result.ok) {
                setError((result && result.message) || 'Step failed.');
                nextBtn.disabled = false;
                nextBtn.textContent = original;
                return;
            }
            // Refresh status, advance to next unsatisfied step.
            const fresh = await fetchStatus();
            if (fresh) state.satisfied = fresh;
            state.currentStep = nextUnsatisfiedFrom(state.currentStep + 1);
            renderStep();
            refreshLauncher();
        } catch (err) {
            setError(String(err && err.message || err));
            nextBtn.disabled = false;
            nextBtn.textContent = original;
        }
    }

    function onBack() {
        if (state.currentStep > 1) {
            state.currentStep--;
            renderStep();
        }
    }

    function onSkip() {
        setInfo('Step skipped — underlying state not changed.');
        state.currentStep = Math.min(state.currentStep + 1, TOTAL_STEPS + 1);
        renderStep();
    }

    // ----------------------------------------------------------------------
    // Launcher + modal lifecycle
    // ----------------------------------------------------------------------

    async function refreshLauncher() {
        const btn = document.getElementById('btn-open-calibration-wizard');
        const summary = document.getElementById('wizard-status-summary');
        if (!btn || !summary) return;
        const s = await fetchStatus();
        if (!s) {
            summary.textContent = 'Could not read calibration status.';
            return;
        }
        state.satisfied = s;
        const allKeys = STEPS.filter((st) => st.statusKey).map((st) => st.statusKey);
        const doneCount = allKeys.filter((k) => s[k]).length;
        const total = allKeys.length;
        if (doneCount === total) {
            btn.textContent = 'Calibrated \u2713';
            btn.classList.add('is-calibrated');
            btn.classList.remove('btn-outline-primary');
            btn.classList.add('btn-outline-success');
            summary.textContent = 'All steps satisfied. Open the wizard to re-run.';
        } else {
            btn.textContent = 'Run calibration wizard';
            btn.classList.remove('is-calibrated');
            btn.classList.add('btn-outline-primary');
            btn.classList.remove('btn-outline-success');
            summary.textContent = `${doneCount} of ${total} steps satisfied.`;
        }
    }

    async function onModalShow() {
        clearNotices();
        state._arucoConfirmed = false;
        state._roiCorners = null;
        const s = await fetchStatus();
        state.satisfied = s || {};
        state.currentStep = nextUnsatisfiedFrom(1);
        renderStep();
    }

    function wireUp() {
        const nextBtn = document.getElementById('wizard-btn-next');
        const backBtn = document.getElementById('wizard-btn-back');
        const skipBtn = document.getElementById('wizard-btn-skip');
        if (nextBtn) nextBtn.addEventListener('click', onNext);
        if (backBtn) backBtn.addEventListener('click', onBack);
        if (skipBtn) skipBtn.addEventListener('click', onSkip);

        const modalEl = document.getElementById('calibration-wizard-modal');
        if (modalEl) {
            modalEl.addEventListener('shown.bs.modal', onModalShow);
        }

        refreshLauncher();
        // Re-poll the launcher state periodically while the dashboard is
        // visible, so the "Calibrated" badge updates as the user fiddles
        // with things elsewhere in the UI.
        setInterval(refreshLauncher, 15000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wireUp);
    } else {
        wireUp();
    }

    // Expose for tests / debugging.
    window.__calibrationWizard = { STEPS, state, fetchStatus };
})();
