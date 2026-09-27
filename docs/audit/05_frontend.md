# 05 — Frontend audit (static/, templates/, lint tooling, design-system plans)

Branch: `docs/audit-and-backlog` · HEAD `dcdef14` · audited 2026-09-27
Scope: every JS/CSS file under `static/` (fonts/PNG skipped), every template under `templates/`, `eslint.config.js`, `.prettierrc.json`, `package.json`, `plans/design-system/`, `plans/ooux_inventory.md`, `plans/right_click_menu_plan.md`. I grepped `web_server.py` and the worker modules only to map routes and socket events. I did not change any code.

**Context:** a hardware redesign will replace the staging platform with an upward-facing second camera. Cards will be imaged while the suction head holds them. Section 4 separates the work this **forces** from work that is only **nice to have**.

---

## 1. Structure

### 1.1 Pages

| URL | Template | Purpose |
|---|---|---|
| `/` | `templates/index.html` + `partials/*` | Single-page operator UI. Bootstrap tabs: Home / Sort / Collection / Setup. Also a Settings modal, the calibration wizard, the Moxfield modal and the Past sessions modal. |
| `/tome` | `templates/tome.html` | The "Tome of Knowledge" operator manual. All 16 sections are "Coming soon" stubs. |
| `/label` | `templates/label.html` | Foil labeler, a dataset tool. Standalone inline script, Bootstrap **5.3.2** (the main app uses 5.3.3). |
| (separate app) | `templates/frame_label.html` | Served by `frame_label_tool.py` on :5055 (`/api/next`, `/api/stats`, `/api/label`, `/api/image`), not by `web_server.py`. It is a dev tool kept in the production templates dir. |

### 1.2 Script architecture

- There is no bundler and no ES modules. `partials/_scripts.html` loads 28 classic `<script>` tags in a fixed order: util → errors → components → api → theme → state → estop → camera → motion → bins → sortConfig → session → simulation → collection → boxes → cull → locator → wishlist → moxfield → sessionHistory → review → calibration → enrichment → sortStage → home → tour → help → socket → app → calibration_wizard.
- Every function is global. Templates wire behaviour through inline `onclick="foo()"` attributes, and so does HTML built by JS. Modules reach into each other's globals directly: for example `simulation.js` mutates `_motionWaypoints`, `_pendingBinUpdates` and `_cachedBinCounts`, which are owned by `motion.js` and `bins.js`.
- Third-party code comes from CDNs at runtime: Bootstrap CSS+JS (jsDelivr), socket.io 4.7.5 (cdn.socket.io), **d3 v7 (d3js.org, never used)**, and Google Fonts. **The kiosk UI will not work offline** because Bootstrap and socket.io are both CDN-only.
- `api.js` monkey-patches `window.fetch` so that every non-GET request carries `X-Requested-With`, which satisfies the CSRF check in `web_server.py:405`.

### 1.3 How state flows

Shared state lives in module-level globals. There is no store.

| Global | Owner | Written by |
|---|---|---|
| `currentState`, `sessionActive` | state.js | socket `sorter_state`, app.js boot poll, motion.js reads |
| `motionState`, `_motionDisplay`, `_motionWaypoints`, `_pendingBinUpdates` | motion.js | socket.js (`motion_path`, `motion_update`, `session_stats`, `sim_progress`), simulation.js |
| `_cachedBinLocations`, `_cachedBinCounts`, `_machinePositions` | bins.js | bins.js, session.js, motion.js, simulation.js, socket.js |
| `_continuousActive`, `_undoAvailable` | session.js | socket.js, state.js reads |
| `_scFilename/_scDirty/_scBuiltin` | sortConfig.js | session.js reads |
| `_inventoryPage` | collection.js | boxes.js, review.js, socket.js read |
| `_calLockedMarkers` | calibration.js | socket.js writes |
| `_currentStage` (pre/running/post) | sortStage.js | socket.js hooks |

Data arrives in three ways:
1. **Socket.IO push**: about 60 handlers in `socket.js`.
2. **Tab-show REST loads**: `shown.bs.tab` hooks in `app.js`, `calibration.js` and `tour.js`.
3. **Polling timers**:
   - `/api/camera/status` every 1 s (camera.js:72), plus a second poll of the same endpoint every 5 s (app.js:106).
   - `/api/motion/position` every 2 s while sorting (motion.js:171).
   - `/api/calibration/status` every 15 s (calibration_wizard.js:591).
   - `/api/sim/status` every 1 s while a simulation runs.
   - The wizard polls `/api/calibration/status` every 0.8 s while waiting on a step.

The Sort tab's layout switches on `#tab-sort[data-sort-stage]` combined with a `[data-stage~=…]` CSS rule (style.css:107).

### 1.4 REST endpoints used (by module)

| Module | Endpoints |
|---|---|
| app.js | GET `/api/status`, `/api/camera/status` |
| api.js / errors.js | (wrapper) · GET `/static/errors.json` |
| estop.js | POST `/api/estop`, `/api/reset-after-estop` |
| camera.js | GET `/api/camera/status`, `/api/camera/feed` (MJPEG), POST `/api/camera/start` |
| motion.js | GET `/api/motion/position` |
| bins.js | GET/POST `/api/bins/config`, GET `/api/bins/fullness`, POST `/api/bins/card-limit`, `/api/bins/locations`, `/api/bins/machine-positions`, `/api/bins/mark-empty`, `/api/bins/test/<n>`, `/api/bins/probe/<n>`, GET/POST `/api/bins/overflow`, GET/POST/DELETE `/api/bins/saved-configs[/<f>]`, POST `/api/motion/move-x` |
| sortConfig.js | GET `/api/sort/configs`, GET/POST/DELETE `/api/sort/configs/<f>`, POST `/api/sort/configs/<f>/duplicate`, POST `/api/sort/validate-query`, GET `/api/translate/scryfall` |
| session.js | POST `/api/session/start`, `continuous/start`, `continuous/stop`, `undo`, `wishlist-bin`, `priority-bin`, `rehome-interval`, `discard-stale`, `resume-stale`, `resume`; POST `/api/test-scan/start`, `/stop`; GET `/api/integrations/moxfield/wishlist/list`; GET `/api/source-bin/state`; POST `/api/self-test/run` |
| simulation.js | POST `/api/sim/test-run`, GET `/api/sim/status` (+ POST `/api/sim/stop` inline) |
| collection.js | GET `/api/collection/stats`, `/boxes/summary`, `/inventory`, `/filter`, POST `/inventory`, `/inventory/<id>/increment`, DELETE `/inventory/<id>`, DELETE `/sessions/<id>`, POST `/reset`, POST `/import/csv`, GET `/api/cards/autocomplete`, `/api/cards/printings`; external `api.scryfall.com` images |
| boxes.js | GET `/api/collection/boxes`, PUT `/api/collection/inventory/<id>/box` |
| cull.js | GET `/api/collection/cull-candidates`, `/export` |
| locator.js | POST `/api/locator/query` |
| wishlist.js | GET/POST `/api/collection/wishlist`, DELETE `/<id>`, POST `/<id>/found` |
| moxfield.js | POST `/api/integrations/moxfield/wishlist/paste`, GET `/export-text` |
| sessionHistory.js | GET `/api/collection/sessions`, `/api/database/status`, POST `/api/database/check-update` |
| review.js | GET `/api/collection/review-queue`, POST `/<id>/confirm`, `/dismiss`, `/correct`; GET diagnostics URL; GET `/api/detection-reviews/counts`, `/<var>`, POST `/seed`, PATCH `/<id>/verdict` |
| calibration.js | POST `/api/calibration/start`, `cancel`, `new-hardware-setup`, `retry`, `last-setup/reload`, `last-setup/save-as`, `last-setup/load`, `last-setup/delete`, `detect-markers`, `check-empty`, GET/POST `offset`, GET `source-bins`, `last-setup/list`, `drop-tuner/status`, POST `drop-tuner/{start,pickup,step-z,test-drop,save,cancel}`; MJPEG `/api/camera/feed-aruco` |
| calibration_wizard.js | POST `/api/connect`, `/api/camera/start`, `/api/calibration/start`, `/api/bins/probe-all`, **`/api/session/set-staging-roi`**, `/api/calibration/wizard/lock-focus`; GET `/api/calibration/status` |
| enrichment.js | POST `/api/collection/prices/update`, `/stop`; GET `/api/enrichment/sources`, POST `/refresh/<name>`; GET `/api/sort/current`, `/api/bins/fullness` |
| sortStage.js | GET `/api/collection/sessions?limit=25`, `/api/detection-reviews/recent` |
| home.js | GET `/api/status`, `/api/collection/stats`, `/api/collection/sessions?limit=3`, `/api/calibration/last-setup` |
| Templates (inline) | POST `/api/connect`, `/disconnect`, `/home`, `/home/x`, `/home/z`, `/motion/move-x`, `/motion/move-z`, `/motion/detection-position`, `/camera/start`, `/camera/stop`, `/database/refresh-prices`, `/database/update`, `/database/update/stop`, `/bins/probe-all`, `/source-bin/calibrate-empty`, `/session/detect`, `/pause`, `/resume`, `/stop`; links `/api/calibration/generate-markers`, `/api/collection/boxes/labels`, `/export/csv`, `/export/decklist`, `/api/support/bundle` |

**Routes with no UI caller.** These are candidate dead routes, or features that have no UI yet: `/api/bins/contents`, `/api/calibration/max-sweep`, `/api/camera/snapshot`, `/api/collection/boxes/manage[/<x>]`, `/api/collection/dividers[/<x>]`, `/api/collection/locate`, `/api/collection/prices/status`, `/api/enrichment/card/<x>`, `/api/enrichment/probes/run`, `/api/integrations/moxfield/deck/import`, `/deck/<x>/cache`, `/wishlist/import`, `/wishlist/upsert`, `/api/review/lookup`, `/api/session/scan-images/<x>`, `/api/session/staging-snapshot`, `/api/session/status`, `/api/sort/modes`, `/api/label/stats`, `/api/openapi.json`, `/docs`.

Box and divider management and Moxfield deck import exist on the backend but have **no UI**. The OOUX inventory lists them as Collection-tab actions.

### 1.5 Socket.IO events

| Event | Handler location | Emitted by backend? |
|---|---|---|
| connect / disconnect | socket.js:7,12 | builtin (no state resync on reconnect) |
| sorter_state (**2 handlers**) | socket.js:21, 125 | yes |
| hardware_status | socket.js:41 | yes |
| estop_triggered | socket.js:67 (blocking `alert`) | yes |
| stale_session_detected / _discarded / _resumed | socket.js:74-93 | yes |
| bin_full_prompt | socket.js:97 | yes |
| source_bin_count_update / source_bin_calibrated | socket.js:104-109 | yes |
| self_test_started / _step / _complete | socket.js:112-120 | yes |
| card_detected (**4 handlers**) | socket.js:135, 197, 201, 936 | yes |
| card_picked_up / card_dropped | socket.js:231, 237 | yes |
| session_stats | socket.js:243 | yes |
| session_started (**2**) / session_ended (**2**) | socket.js:283, 547 / 290, 530 | yes |
| **staging_capture_prompt** | socket.js:295 | **no: dead handler** |
| staging_roi_prompt | socket.js:323 | yes (web_worker.py) |
| staging_capture_result | socket.js:478 | yes |
| focus_confirm_prompt | socket.js:494 | yes |
| **bin_update** | socket.js:551 | **no: dead** |
| probe_result | socket.js:555 | yes |
| test_scan_progress / _card / _finished | socket.js:560-584 | yes |
| motion_path / motion_update | socket.js:590, 604 | yes |
| bins_configured, bin_full, bin_emptied, overflow_map_updated | socket.js:622-651 | yes |
| **bins_chain_full** | socket.js:644 | **no: dead** |
| sim_progress / sim_card_sorted (empty body) / sim_complete | socket.js:657-681 | yes (web_motion_sim.py) |
| db_update_progress | socket.js:687 | yes (web_database.py) |
| log_message | socket.js:712 | yes |
| calibration_marker_visible, hardware_setup_progress, hardware_setup_complete, calibration_progress, calibration_complete, calibration_marker_found, calibration_rejections, bin_empty_check, source_bins_status | socket.js:723-878 | yes |
| price_update_progress | socket.js:884 | yes |
| continuous_sort_started / _stopped, sort_undone, wishlist_match, bin_fullness_warning | socket.js:913-964 | yes |
| enrichment_refresh_started / _progress / _complete | socket.js:970-985 | yes |
| drop_tuner_progress / _complete / _error | socket.js:991-1057 | yes |

**Emitted but not handled:**
- `worker_crashed` and `worker_fatal` (web_worker.py). **The operator gets no signal when the worker dies.**
- `session_paused`. This includes `{'reason': 'serial disconnected'}` at web_worker.py:1449.
- `estop_reset_complete`
- `probe_all_complete`
- `camera_health`
- `calibration_marker_refined`
- `test_scan_started`
- `error`

---

## 2. Per-file summary

| File | Lines | Summary |
|---|---|---|
| static/app.js | 125 | Boot: theme, initial loads, tab-show hooks, `/api/status` poll, a 5 s camera-status poll that duplicates camera.js, sort-config and bin-routing loads. |
| modules/util.js | 19 | **Three** HTML-escape functions (`escapeHtml`, `_esc`, `_escapeHtml`) with slightly different semantics. |
| modules/api.js | 100 | fetch CSRF shim, `apiGet` (no error handling, no `resp.ok` check), `apiPost` (toast on error, returns `{ok:false,...}`). |
| modules/errors.js | 122 | Toast renderer driven by `errors.json` (32 codes, one of which is `no_staging_configured`). Toasts have no `role="alert"`/aria-live, and warning and error toasts never auto-dismiss or stack-limit. |
| modules/components.js | 143 | `renderEmptyState`, `renderSkeleton`, `withSkeleton` (unused). The CTA uses an inline `onclick` string. |
| modules/theme.js | 25 | Light/dark toggle. It sets `btn.textContent`, which overwrites the ☼ glyph. |
| modules/state.js | 72 | State badge, session-button enablement, activity log (100 entries, prepends). |
| modules/estop.js | 37 | E-stop POST (fire-and-forget, no error handling). **A global `Escape` key listener triggers E-STOP.** Reset-after-estop uses `confirm` and `alert`. |
| modules/camera.js | 226 | 1 s camera-health poll → navbar badge; `showCameraDetails` shows an `alert`; overlay canvas banner; Live Card Info panel; `startCameraFeed`/`stopCameraFeed` (unused `_feedIntervals`). Single-camera model. |
| modules/motion.js | 532 | Waypoint animation (rAF), 2 s position poll, `drawMotionCanvas` (side view **with staging platform, camera FOV to platform**), `drawBinLayoutCanvas` (top view with staging). Hard-coded colours, X_MAX=1100, Z_MAX=220. |
| modules/bins.js | 323 | Bin table with **Staging X/W and Camera Offset rows**, machine positions (`staging_x`, `staging_width`), even-spacing config, saved bin configs, overflow chains. |
| modules/sortConfig.js | 705 | Preset parser/serializer, table editor, Scryfall preview, validation (per-keystroke POST with no debounce), save/save-as/duplicate/delete via `prompt()`, lock while active, two mirrored preset selects. |
| modules/session.js | 688 | Test scan, `startSession`, bin tile grid (full re-render), continuous/undo, priority toast, wishlist/priority/re-home setters, storage plan, stale-session banner, bin-full banner, source-bin stack estimate, self-test rendering. |
| modules/simulation.js | 85 | Sim start plus a 1 s poll that runs alongside socket events. |
| modules/collection.js | 659 | Stats, box summary, inventory table (string concat), pagination, filter chips, sort, legacy `searchCollection` (drops filters and pagination), add card with autocomplete, hover preview (Scryfall remote images), import/reset/bulk select, collection sub-nav. |
| modules/boxes.js | 132 | Bulk and single box-assign overlays built by hand (not Bootstrap modals), plus the box list. |
| modules/cull.js | 80 | Cull candidates table and CSV export. |
| modules/locator.js | 115 | Locator query and grouped rendering. |
| modules/wishlist.js | 61 | Wishlist CRUD table. |
| modules/moxfield.js | 77 | Paste import and export text. |
| modules/sessionHistory.js | 78 | Sessions table, DB info, update check. |
| modules/review.js | 545 | Legacy hash-distance review queue (Collection tab), correction dialog, hash-diagnostics overlay; per-attribute detection review queue (Setup tab). |
| modules/calibration.js | 507 | ArUco calibration start/cancel/new-HW-setup/retry (all send `expect_staging`), live ArUco MJPEG toggle, saved setups, locked-marker table, detect markers, camera offset, source bins, drop tuner. |
| modules/enrichment.js | 203 | Price update, enrichment sources panel and progress, Bin Routing table on the Sort tab. |
| modules/sortStage.js | 334 | pre/running/post stage machine, a11y announcements, post-sort summary, past-sessions modal, post-sort review preview, `onUnfinishedSessionDetected` (a stub). |
| modules/home.js | 237 | `goToTab`, home status strip (four sequential awaits, not parallel), recent activity, getting-started banner. |
| modules/tour.js | 336 | Five-step first-run overlay tour. It has its own `escapeHtml` and its own keydown listener. |
| modules/help.js | 59 | Initialises popovers on load, on every tab show and on every modal show. `sanitize:false`. |
| modules/socket.js | 1057 | All socket handlers (§1.5), including two big hand-built overlays: **Staging ROI picker** and **Focus confirm**. |
| static/calibration_wizard.js | 602 | Seven-step wizard. Steps 3, 4, 6 and 7 assume a staging platform (marker 49, `expect_staging`, `set-staging-roi`, "slide a card onto the staging platform"). Its ROI picker duplicates the one in socket.js. |
| static/style.css | 2161 | Semantic tokens, dark/light themes, Bootstrap overrides, stage visibility rule, bin tiles, empty/skeleton styles, home page, heroes, tour, help popover. It contains mojibake (`â€”`) in comments and a fair amount of dead CSS (§3.2). |
| static/tokens.css | 157 | Raw palette, spacing, radius, type, shadow, motion, z-index tokens; self-hosted Citadel of Blackrose font. |
| static/errors.json | 224 | Error catalogue: title, what_happened, what_to_try, severity. |
| static/branding/ | — | Five sigil SVGs (used). `cardomancer-logo.png` is **not referenced anywhere**. There is no favicon. |
| templates/index.html | 38 | Shell. |
| partials/_navbar.html | 23 | **Contains a stray second `<body>` tag (line 1).** Theme and settings buttons, three badges, E-STOP, Reset & Re-home. |
| partials/_tabs_nav.html | 15 | Four tabs; the Setup tab carries an inline `onclick` and the review badge. |
| partials/_macros.html | 50 | `help_btn` macro (HTML in `data-bs-content`). |
| partials/_modals.html | 162 | Calibration wizard ("6. Staging ROI" in the rail), Moxfield, Past sessions. |
| partials/_tab_settings_modal.html | 197 | Appearance (placeholder), Data & Sources, Card data, Support bundle, About. |
| partials/_tab_home.html | 273 | Backdrop of seven inline copies of the same SVG, hero, status strip, three tiles, recent activity, getting-started banner, tour link, footer. |
| partials/_tab_sort.html | 896 | Post hero, pre hero, Sort Configuration (includes a large inline query reference), Bin Routing, bin config (**Staging X/W inputs**), bin layout canvas plus the priority toast stack, live readout (camera, controls, suction head, last card, card info, stats, source bin, bin tiles), activity log, Quick Stats and Test Scan. |
| partials/_tab_collection.html | 423 | Hero, sub-nav (Inventory / Locator / **Sync, which has no section** / Cull), inventory, cull, locator, session history, legacy review queue, wishlist. |
| partials/_tab_setup.html | 516 | Hero, wizard launcher and diagnostics, Hardware (**hard-coded "COM3"**), Camera, Manual Motion (duplicate Home buttons, "Detection Pos"), Camera Test, Live ArUco, ArUco Auto-Calibration (**"Expect staging platform marker (ID 49)"**), Calibration Settings (camera X offset), Drop tuner, Session Review, hidden Motion Preview and Simulation. |
| templates/tome.html | 364 | Manual shell. Every section is a stub. It does not load the Cormorant font that `--font-display` falls back to. |
| templates/label.html | 343 | Foil labeler. Session names are interpolated into HTML and URLs without escaping. |
| templates/frame_label.html | 205 | Frame-era labeler for the separate :5055 tool. |
| eslint.config.js | 83 | Flat config, `sourceType: script`, most rules at warn. It ignores `static/otag_explorer.js`, which no longer exists. Globals list is incomplete (`io`, `bootstrap` only; `CSS`, `performance`, `Headers` missing). |
| .prettierrc.json / package.json | 34 / 17 | Lint and format scripts only. There is no lockfile, no CI hook and no test runner. |
| plans/design-system/* | — | Claude Design handoff: README (brand rules), SKILL, HANDOFF (points to a `chats/` dir that is not in the repo), `colors_and_type.css` (a second token set with different semantic names, e.g. `--fg-primary` vs `--text-primary`). |
| plans/ooux_inventory.md | 270 | Object model. It defines Box/BoxDivider management and "re-sort this box", neither of which has a UI. |
| plans/right_click_menu_plan.md | 205 | Context menu plan. It depends on `POST /api/bulk-edit/apply`, **which does not exist** in web_server.py. |

---

## 3. Findings

### 3.1 Bugs

| # | Sev | Location | Finding |
|---|---|---|---|
| B1 | **High** | estop.js:13-17 | A global `keydown Escape` fires `emergencyStop()`. Escape is also the standard key for closing Bootstrap modals, the tour (tour.js:78), popovers and the hand-built overlays. **Closing any dialog with Esc e-stops the machine** mid-sort and forces a re-home. Either require a deliberate key (e.g. Shift+Esc or Space held) or skip when a modal/overlay is open. |
| B2 | **High** | _tab_sort.html:629-740 vs 591 (`data-stage="running post"`) | These controls sit inside the running/post-only section, so **none of them are visible in the pre-sort stage**, which is when the operator needs them: session notes, destination box, starting divider, re-sort toggle and source box, the dashboard preset select, **Wishlist bin, Priority bin, Re-home interval**. `startSession()` (session.js:53-63) reads the notes, storage and resort values from those hidden inputs. The operator can only fill them after the session has started, when they are no longer used. |
| B3 | High | _tab_sort.html:560 (inside `data-stage="pre"`) | `#priority-toast-stack`, which shows wishlist-match alerts, lives in the pre-only bin-layout card. Priority toasts fire **during running**, so they render into a hidden element and the operator never sees them. |
| B4 | High | socket.js / web_worker.py | `worker_crashed` and `worker_fatal` have no handlers. A worker death shows no banner or toast. `session_paused` has no handler either, so `reason: serial disconnected` is only visible as a state-badge change. |
| B5 | Med | _tab_collection.html:162 | `#bulk-actions` has both `class="d-flex"` and `style="display:none"`. Bootstrap's `.d-flex{display:flex!important}` wins, so the "0 selected / Assign to Box" bar is always visible. |
| B6 | Med | _tab_collection.html:19 | The sub-nav "Sync" links to `data-collection-view="sync"`, but no `data-collection-section="sync"` exists. Clicking it hides every section and leaves a blank page. |
| B7 | Med | socket.js:260-268 vs session.js:566-612 | `session_stats` overwrites `#stack-estimate-panel` with a different "Est. cards remaining / time" layout, which clobbers the source-bin probe gauge that `source_bin_count_update` renders. Two writers own one panel. |
| B8 | Med | cull.js:8, 77 | `parseFloat(x) ?? 0.05`. NaN is not nullish, so an empty field sends `max_buylist=NaN`. |
| B9 | Med | collection.js:310-335 | `searchCollection()` (the Search card) ignores the filter chips and pagination. It renders every result on one page and does not reset `_inventoryPage`. It also duplicates `loadInventory`. |
| B10 | Med | app.js:61-79, socket.js:283-288 | `startCameraFeed('session-camera-feed')` runs on every Sort-tab show and every `session_started`, and POSTs `/api/camera/start` each time. The feed element is hidden in the pre stage, so an MJPEG stream downloads invisibly. `stopCameraFeed` only runs on `session_ended`, never on tab hide. |
| B11 | Med | calibration_wizard.js:81, 188, 230 | Wizard steps inject `<img src="/api/camera/feed-aruco">` and `/api/camera/feed` into the modal. Nothing clears `src` on `hidden.bs.modal` or when the step changes, so the streams keep running until the element is replaced or the page reloads. |
| B12 | Med | calibration.js:81-96 | The live ArUco MJPEG starts automatically on **every** Setup tab show. The code comment admits this. |
| B13 | Med | socket.js:67-70 | `estop_triggered` calls a blocking `alert()`, which freezes all socket handling until it is dismissed. The same pattern appears in camera.js:69, review.js, boxes.js and elsewhere. |
| B14 | Med | socket.js:7-15 | On reconnect nothing re-fetches `/api/status`, session stats or the stage. After a server restart the UI can show a stale state or stage until the next event. |
| B15 | Med | session.js:90 vs bins.js:50, enrichment.js:86 | The bin capacity fallback is 300 in the tiles and 150 in the table and routing. The source-stack gauge uses a hard-coded `softMax = 300` (session.js:598). |
| B16 | Low | calibration_wizard.js:117 vs _tab_setup.html:216 vs calibration.js:8 | The default destination count differs: 7 in the wizard, 7 in the Setup HTML, and a JS fallback of 10. |
| B17 | Low | _navbar.html:1 | A duplicate `<body>` tag, which is invalid HTML. |
| B18 | Low | theme.js:13 | The toggle glyph changes from ☼ (HTML) to ☆/☾. There is no persistent `aria-pressed`. |
| B19 | Low | home.js:218 | The recent-activity click calls `openPastSessionsModal(id)`, but the function ignores the id. It always opens the last-25 list instead of that session. |
| B20 | Low | sortStage.js:301 | `goToReviewQueue` goes to the Setup tab. `loadDetectionReviewQueue` then only refreshes if the pane is already active, which is a race. The comment says Setup's onclick loads counts, but that only happens for nav-link clicks. |
| B21 | Low | wizard step 6 / socket.js:327 | The ROI picker assumes a 1080×1920 fallback when naturalWidth is 0. It uses `canvas.width` as the CSS size, which is fine only because `sync()` sets it on every MJPEG frame (a resize per frame). |
| B22 | Low | review.js:131, collection.js:376 | `'${name.replace(/'/g,"\\'")}'` inside an `onclick` attribute. A card name containing `"` or `&` breaks the attribute. |
| B23 | Low | label.html:166-176 | Session names are interpolated into `<option>` and fetch URLs unescaped and un-encoded. |
| B24 | Low | tome.html:8 | Loads Inter and JetBrains but not Cormorant. The Citadel font is available via tokens.css, so this only matters as a fallback. |
| B25 | Low | eslint.config.js:78 | Ignores the removed `otag_explorer.js`. There is no lint step in CI. |

### 3.2 Security and XSS

The server is localhost/kiosk, so the risk is low, but data comes from Scryfall, Moxfield pastes, CSV imports and the operator's own notes. The following `innerHTML` sinks use **unescaped** values:

- wishlist.js:22-26 (`w.name`, `w.notes`, `w.set_code`)
- sessionHistory.js:30-32 (`s.notes`, `sort_mode`)
- collection.js:47, 79-81 (`b.box_name`, `item.name`, `type_line`)
- cull.js:56-64
- simulation.js:81
- socket.js:181-185, 570, 1015, 1048, 1216 (`data.name`, `frame_effects`, `data.message`)
- camera.js:185-201 (`name` in `alt=` and body)
- review.js:89, 169, 178, 221, 290-292
- boxes.js:10, 79 (`<option value="${b}">`)
- enrichment.js:43-51 (`s.name` inside an `onclick` string)
- locator.js (escaped, fine)
- label.html, frame_label.html

`help.js` sets `sanitize:false`, but that content is template-authored, so it is safe. Recommendation: one escaping helper, used everywhere, or move to `textContent`/DOM building.

### 3.3 Dead code and duplication

- **Dead socket handlers:** `staging_capture_prompt` (socket.js:295-320), `bin_update`, `bins_chain_full`, `sim_card_sorted` (empty body).
- **Dead JS:**
  - `withSkeleton` (components.js:129)
  - `_feedIntervals` (camera.js:210)
  - `_overlayTimeout` fade (still used)
  - `onUnfinishedSessionDetected` (sortStage.js:328, never called)
  - `getSortStage` (unused)
  - `_dropTunerInFlight` (written, never read)
  - `arucoLiveActive` is fine
  - `searchCollection` duplicates `loadInventory`
  - `window.__calibrationWizard` and `_initHelpPopovers` (debug only)
- **Dead markup:**
  - `#dashboard-preview` img (_tab_setup.html:109, never set)
  - `#sim-mode-display` hard-coded "Color"
  - the entire `#motion-preview-section` (hidden with `display:none` and no toggle). **motion.js still redraws a hidden 800 px canvas on every animation frame and on every motion event.**
  - the d3 script tag
  - the unused `cardomancer-logo.png`
- **Dead CSS:** `#query-reference*`, `#config-file-preview`, `.bin-header`, `.bin-card-entry`, `.bin-section` (the old bin list; `_showCardPreview` still looks for `.bin-section`, collection.js:453), `@keyframes pulse-green`, `.page-link`/`.page-item` (pagination now uses buttons), `.cm-home-status-cell strong.ok/.warn/.bad/.muted` (home.js sets inline colours instead), and `var(--border)` (undefined, style.css:1019).
- **Duplicated logic:**
  - Three escape functions (util.js), plus private copies in tour.js and calibration_wizard.js.
  - **Two staging-ROI pickers** (calibration_wizard.js:405-478 and socket.js:323-476).
  - Two card-name autocompletes (collection.js:358 and review.js:203).
  - Two box-assign overlays (boxes.js:3 and 71).
  - Four hand-rolled `position:fixed` overlays (boxes, review correct, hash diag, staging, focus) instead of one Bootstrap modal helper.
  - Two camera-status pollers (camera.js:72 and app.js:106).
  - Two preset selects (sortConfig.js:233).
  - Two Start Session buttons (pre hero plus the running-stage `#btn-start-session`).
  - Two Home/Home X/Home Z button groups (_tab_setup.html:88-92 and 140-145).
  - Two review systems: the legacy hash-distance "Scan Review Queue" (Collection) and the "Session Review" detection queues (Setup).
  - `sorter_state`, `card_detected`, `session_started` and `session_ended` each have several handlers split across socket.js.
  - Two token files (`static/tokens.css` and `plans/design-system/colors_and_type.css`) with diverging semantic names.
  - Border-colour badge maps in camera.js:153 and socket.js:140.

### 3.4 Accessibility

- **The E-stop Escape binding (B1) is also an accessibility hazard.** Keyboard users rely on Esc.
- Toasts (errors.js, the priority toast) and the banners (stale session, bin full) have no `role="alert"` or `aria-live`, so screen readers never hear bin-full or e-stop.
- Hand-built overlays (boxes, review correct, hash diag, staging ROI, focus confirm) have no `role="dialog"`, no `aria-modal`, no focus trap, no Esc handler and no focus restoration. The tour sets `role=dialog aria-modal` but does not trap focus.
- Clickable `<th class="sortable">` headers and `.box-label` spans are not keyboard reachable and have no `aria-sort`. Filter chips are buttons but carry no `aria-pressed`. The collection sub-nav `<a href="#">` links have no `aria-current`.
- Icon-only buttons with only a `title` (screen readers may skip it): ×, ✓, ✎, 🔍, 🔗, ☰, ⚙, ↻. Examples: wishlist.js:28-29, sessionHistory.js:36, review.js:130-133, sortConfig.js:121-128, collection.js:67-69.
- Many inputs have no `<label>`: search inputs, add-card fields, wishlist fields, manual X/Z (the label exists but has no `for`), cull inputs (no `for`), `#review-threshold` (no `for`), bin-table and overflow inputs built by JS.
- The camera badge is a `<span onclick>` and is not focusable (_navbar.html:12). The Sort Configuration header toggles via `div data-bs-toggle`, which has no role or tabindex.
- Canvas views (motion, bin layout, camera overlay, ROI) have no text alternative. The ROI picker is mouse-only.
- `.cm-home-tile { all: unset }` plus an explicit focus ring is fine. Home `aria-label`s override the visible text ("Begin a sort session." vs "Begin the sorting"), which is a minor WCAG 2.5.3 mismatch.
- Contrast:
  - `.cm-home-footer` #5A586F on #0A0A1C is about 2.9:1 (fail).
  - The badge `bg-warning text-dark` passes.
  - The light theme leaves hard-coded `#fff`/`#ECEAF6` text on heroes that keep dark gradients, which is okay, but the tour and popovers are forced dark, so the light theme is only half-implemented.
- Heading structure: each tab has an `<h2>` hero with no `<h1>` except on Home (`<main>` inside the tab pane, and a second `<main>`-like region absent elsewhere).
- `prefers-reduced-motion` is honoured for the backdrop and skeletons, but not for the E-stop glow animation, the home tile transforms or the motion canvas.

### 3.5 Performance

- **The bin tile grid is fully re-rendered on every `session_stats`** (session.js:84-231). Each tile builds an HTML entry for **every card in the bin** (unbounded: 150–300 per bin × N bins → thousands of nodes each card). On top of that it redraws two canvases, one of them hidden. Fix: diff per tile, cap the detail list (e.g. the last 50), and render lazily on expand.
- `innerHTML +=` inside loops, which is O(n²) re-parsing:
  - wishlist.js:21
  - sessionHistory.js:28
  - review.js:118 (100 rows, each with three images plus hover handlers)
  - collection.js:120, 332 (50 rows)
  - bins.js:142
  - socket.js:572 (test-scan results)
  - socket.js:778 (`el.innerHTML += html`)
- **Continuous MJPEG streams:** the session feed (starts on tab show, B10), live ArUco (starts on Setup show, B12), wizard streams (never stopped, B11), and `/api/camera/feed` in the focus overlay (never cleared on Lock). With the second camera this doubles. There should be a single feed manager that refcounts `<img>` consumers and pauses on `visibilitychange` and tab hide.
- Polling:
  - `/api/camera/status` at 1 Hz **and** 0.2 Hz (duplicate)
  - `/api/calibration/status` every 15 s forever, even when the Setup tab is closed
  - `/api/motion/position` at 0.5 Hz while sorting
  - the sim poll at 1 Hz alongside socket events

  None of them pause when `document.hidden`.
- `drawMotionCanvas` resets `canvas.width`, and therefore reallocates the backing store, on **every animation frame**. It draws even when the canvas is hidden (the parent has `display:none`, so `clientWidth` is 0 and the 800 fallback is used).
- Every card row hotlinks Scryfall images (`api.scryfall.com/...?format=image`, collection.js:427). There is no local cache even though the backend has card images on disk. It is slow and fails offline.
- `/api/sort/validate-query` is POSTed on every keystroke (sortConfig.js:460) with no debounce and no stale-response guard.
- `home.js loadHomeStatus` runs four `await`s in sequence instead of `Promise.all`.
- The Home backdrop runs seven continuously rotating SVGs with `will-change` plus a fixed full-viewport element with `backdrop-filter` on `#mainTabs` on **every** tab, alongside live camera streaming. The design README says "No backdrop-blur on the operator UI (perf-sensitive)".
- `help.js` re-scans the whole document for popovers on every tab and modal show. That is cheap, but it runs unconditionally.

---

## 4. UX assessment

### 4.1 Setup and calibration

- **Two parallel paths.** One is the seven-step wizard modal (Setup launcher and Home banner). The other is the raw Setup tab: New Hardware Setup, Retry, Start calibration, Reload/Save/Load setups, camera offset, drop tuner, manual motion, camera test and live ArUco. They share endpoints but not state or defaults (B16). An operator cannot tell which one is authoritative. The wizard does not cover drop-height tuning, source-bin empty calibration or camera offset.
- The Setup tab is one long scroll of about 12 cards with no grouping by task (Connect → Calibrate → Tune → Review). Session Review (detection queues) is a **results** activity buried under calibration. The Hardware card hard-codes "COM3".
- Calibration progress feedback is split across `#cal-progress-message`, a progress bar, a locked-marker table, a rejections table and the activity log. The activity log only renders on the **Sort** tab, so Setup-tab actions log to a panel the operator cannot see.
- "Machine Positions" (source X, camera offset, **staging X/W**) sits on the Sort tab's pre stage, while "Camera X Offset" sits on Setup. That is the same concept in two places with two inputs (`machine-detect-x` and `cal-camera-offset`).
- **Redesign impact:** calibration currently assumes one downward camera on the carriage, marker 49 on a staging platform, a staging ROI and a staging X/width. All of that changes (see 4.5).

### 4.2 Running a sort

- The pre stage correctly shows the preset hero and Start. But session-scoped options (notes, destination box, re-sort source, wishlist/priority bin, re-home interval) only appear in running (B2). The destination and source box dropdowns are hard-coded placeholders ("Box A — bulk commons") and are not backed by `/api/collection/boxes`.
- The running stage duplicates Start Session (disabled), keeps a manual **Detect & Sort** plus Continuous toggle (the normal flow should be continuous by default), and hides the Bin Routing table only behind the tiles. The **activity log** is the main error channel and sits at the bottom, below the fold.
- Bin-full is communicated two ways: a banner and log lines. Worker crash and serial-drop pause are not communicated at all (B4). E-stop uses a blocking alert.
- Camera panel: a fixed 520 px portrait box with a 5 s overlay banner drawn on canvas. The "Last Card Detected" and "Card Info" panels overlap in content (name, set, price, border shown twice). The "Suction Head" card shows a single word.
- Post stage: a good summary hero and inline review preview. "Review uncertain cards" jumps to Setup rather than an inline review. "View past sessions" can't open a specific session (B19).

### 4.3 Reviewing results

- There are **three review surfaces**:
  1. the post-sort inline preview (read-only, eight items)
  2. the Setup → Session Review detection queues (foil/border/set_symbol, with Correct/Wrong/Skip, where "Wrong" reveals an input only **after** the verdict is already sent: review.js:525-538 reads the still-hidden input's value)
  3. the Collection → Scan Review Queue (hash distance, correct-identification dialog, diagnostics)

  The identity queue lives only in (3). Variable names differ ("identity" is mentioned in the help text but has no tab in (2)).
- Review items show crops, but you have to hover to compare the scan against the reference. Keyboard shortcuts exist only in the separate `/label` tools.

### 4.4 Collection

- The inventory has two competing query UIs: the filter chips plus raw query, and the legacy Search card (B9). They do not compose.
- Box and divider management, "re-sort this box" and Moxfield deck import exist on the backend (and in the OOUX plan) but have no UI. The Sync sub-nav is empty (B6).
- The Locator is good but not linked from inventory rows. There is no card detail view: the Scryfall link is the only drill-down.
- Session History, the legacy review queue and the wishlist render **below** whichever sub-view is active, so the Cull and Locator views are followed by unrelated tables.
- Destructive actions use `confirm()`. Reset Collection uses two confirms, which is fine. Row deletes give no undo.

### 4.5 Changes the redesign forces (the upward second camera replaces the staging platform)

| Area | Current dependency | Required change |
|---|---|---|
| Calibration wizard | calibration_wizard.js:61-88 (marker 49 staging), 126-131 & 141 (`expect_staging`), step 6 (175-217, `set-staging-roi`), step 7 copy ("slide a card onto the staging platform"); _modals.html:32 rail | Remove or replace step 6 with **upward-camera ROI / card-in-gripper framing**. Add "connect camera 2" and per-camera focus lock. Drop marker 49 and the staging checkbox. Rewrite the copy. |
| Setup tab | _tab_setup.html:224-225 staging checkbox; 144 "Detection Pos"; 276-284 camera X offset semantics; Camera card/Camera Test/Live ArUco assume one camera | Show a camera selector or two feeds (downward ArUco camera and upward ID camera), per-camera start/stop/status/fps, and a new "pickup-to-camera position" calibration in place of detection X offset. |
| calibration.js | lines 10, 106, 152 send `expect_staging`; 192, 276 display `summary.staging` | Remove. |
| socket.js | `staging_roi_prompt` overlay (323-476), `staging_capture_result` (478), dead `staging_capture_prompt` (295); `focus_confirm_prompt` (494, single feed); `hardware_setup_complete`/`calibration_progress`/`calibration_complete` staging strings (757, 800, 815) | Delete the staging overlays. Parameterise the focus prompt by camera. Handle new events (e.g. upward-camera capture failure or retry). |
| Sort tab machine positions | _tab_sort.html:510-516 Staging X/W; bins.js:7-26, 55-80, 223-237, 248-256 | Replace with the upward-camera X (the carriage stop above camera 2) and remove staging width. |
| Motion and layout canvases | motion.js:230-306 (platform, camera FOV to platform), 484-506; legend _tab_setup.html:462-463 | Draw the upward camera below the rail and a dwell point. Remove the platform. |
| Live feed | `#session-camera-feed` single `/api/camera/feed`; camera.js overlay; camera badge single-camera | During running, show the **upward camera** (the card in the gripper), optionally the downward one too. Two health badges, a per-camera `/api/camera/<id>/…` API, and the overlay drawn on the correct feed. |
| Copy and errors | session.js:102 "Drop a stack on the staging platform"; errors.json `no_staging_configured`; Tome sections; design-system README ("staging altar") | Rewrite the copy. Replace the error codes (e.g. `camera2_not_active`, `no_camera2_position`). |
| Detection review / crops | crop thumbnails assume staging-platform crops | Likely no UI change, but verify the aspect ratio and orientation of upward crops (the 1080×1920 portrait fallback assumptions in the ROI and camera box). |

### 4.6 Prioritized improvements

**Forced by the redesign** (do these when the hardware lands):
1. Remove the staging concept end to end: wizard steps 3/4/6/7, the Setup checkbox, the machine-positions inputs, the canvases, the socket overlays and the copy (table 4.5).
2. Build a multi-camera feed layer: a camera id in the feed and status API calls, a feed manager that starts and stops streams by visibility, two health badges, and a per-camera focus lock.
3. New calibration for the upward camera: the X/Z dwell position above camera 2, a framing/ROI check with a card held, and focus. It should live in the wizard; the scattered Setup cards should not duplicate it.
4. Update the running-stage live view to show the upward capture (with the overlay) and make the "Last card / Card info" panels a single panel.
5. Delete the duplicated ROI picker code (it disappears naturally with item 1).

**Nice to have, but high value and cheap** (safety or correctness; consider doing these before the redesign):
6. Fix the E-stop Escape binding (B1).
7. Surface `worker_crashed`, `worker_fatal` and `session_paused` (B4), and resync state on reconnect (B14).
8. Move the session options into the pre stage (B2) and make the priority toasts visible (B3).
9. Small fixes: bulk-bar visibility (B5), Sync tab (B6), stack panel clobber (B7), cull NaN (B8).
10. One escaping helper, and fix the XSS sinks (§3.2).

**Nice to have** (long term):
11. Performance: incremental bin tiles, stop hidden canvas drawing, debounced validation, pause polling when hidden, local card images instead of Scryfall hotlinks.
12. Self-host Bootstrap, socket.io and the fonts so the kiosk works offline, and drop d3.
13. Consolidate review into one "Review" surface (identity plus attributes) reachable from post-sort.
14. Restructure Setup into task groups (Connect / Calibrate / Tune / Diagnose) and move Session Review out.
15. Collection: merge Search into the filter chips, build the Box/Divider UI and "re-sort this box", scope sub-view sections correctly, add the right-click menu (which needs the missing bulk-edit API first).
16. Accessibility pass: dialogs as Bootstrap modals with focus traps, aria-live toasts and banners, labels, keyboard-sortable headers.
17. Code health: ES modules or at least namespaced objects, `addEventListener` instead of inline `onclick`, ESLint in CI, one token file, remove dead CSS/JS/markup, fix the mojibake.
18. Write the Tome content, decide the voice register (the operator UI uses "ritual" copy, which contradicts the design-system README's Register A), and finish or remove the light theme.

---

## 5. TODOs and stubs found

| Location | Note |
|---|---|
| calibration.js:83-86 | "A future polish pass should gate this [ArUco live stream] on the calibration sub-section actually being scrolled into view." |
| sortStage.js:322-334 | `onUnfinishedSessionDetected`: "Phase 4 part 5 — full Resume rehydration… stub the hook". Never called. |
| sortStage.js:147-150 | "session_ended doesn't yet carry a reason; show the timestamp for now". The end reason is never shown. |
| session.js:396-397, 483-486 | The stale-session banner is "Phase 2", to be replaced by a Phase 4 limbo state. Remaining stale sessions only show after a reload. |
| socket.js:673-675 | `sim_card_sorted`: "Could append to results list in real-time" (empty handler). |
| camera.js:148-149 | "Tags, salt, combos, and deck-usage are deferred to a future drawer." |
| _tab_sort.html:864-870 | "Test Scan ultimately belongs on Setup; for now it stays gated to pre-sort here." |
| _tab_settings_modal.html:23-26 | "Additional appearance controls are coming." |
| tome.html (all 16 sections) | "Coming soon" stubs; "Full grammar reference forthcoming." |
| eslint.config.js:8-9 | "As we migrate to ES modules + addEventListener (Phase 5), tighten this config." |
| _tab_setup.html:449 | Motion Preview "now hidden by default", with no way to un-hide it. |
| plans/right_click_menu_plan.md | Not started. Depends on the nonexistent `/api/bulk-edit/apply`. |
| plans/ooux_inventory.md | Box/Divider management and re-sort-box have no UI. The user card-sort validation is deferred. |
| plans/design-system/HANDOFF.md | References `cardomancer-design-system/chats/`, which is not in the repo. |
