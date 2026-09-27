# Audit 02: Application Core (server, worker, camera, persistence)

Scope: `web_server.py` (5,514 lines), `web_worker.py` (4,359), `web_camera.py` (883), `web_database.py` (441), `main.py` (347), `config.py` (171), `support_bundle.py` (340), `update_all.py` (426), `scan_tracker.py` (531), `requirements.txt`, `package.json`, `pytest.ini`, `.env.example`, `.gitignore`, `tools/`, `templates/`, `docs/openapi*.json` (skimmed), `README.md`, `PROJECT.md`.
Motion and G-code internals (`gcode_control.py`) belong to another audit. I cite them here only where the application core depends on them.

Audited on branch `docs/audit-and-backlog` (HEAD `90fd3ef`). No code was changed.

---

## 1. Architecture overview

### 1.1 Processes and threads

There is one Python process (`python web_server.py`). Everything below is a thread inside it.

| Thread | Created at | Role |
|---|---|---|
| Main / Werkzeug | `web_server.py:5509` `socketio.run(..., host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)` | HTTP + Socket.IO (`async_mode='threading'`, `web_server.py:356`). Each request runs on its own thread, and each open MJPEG stream holds one thread for as long as it is open. |
| Worker | `web_worker.py:223-229` | The only thread that should touch serial. Pulls `(command, kwargs)` from a `queue.Queue(maxsize=50)` and dispatches to `_cmd_<name>`. |
| Identification (per card) | `web_worker.py:1770` | A short-lived thread that runs card-back check, hybrid ID, disambiguation and foil detection, overlapped with `pick_from_staging()`. |
| Camera capture | `web_camera.py:145` | Loops `cap.read()`, rotates the frame 90 degrees and stores it under `_lock`. |
| Camera watchdog | `web_camera.py:150` | Runs once a second. Marks health stalled or dead and forces a reconnect. |
| Camera-health grace timer | `web_server.py:5423` | Auto-pauses the sort after 12 s of unhealthy camera. |
| DB updater | `web_database.py:143,165` | Scryfall bulk download, images, v1/v2 hashes. |
| Price updater | `web_server.py:4419` | Re-prices inventory from the bulk JSON. |
| Enrichment scheduler | `web_server.py:468,5503` | APScheduler cron jobs (Tagger, EDHREC, and others). |
| Motion simulator | `web_motion_sim.simulator` | Sim runs started from `/api/sim/test-run`. |

Several paths bypass the worker queue and write straight to serial from a Flask thread: `emergency_stop()` (`web_worker.py:335-341`), `_graceful_shutdown` (`web_server.py:5058`) and `_emergency_safety_reset`. `gcode_control.py` has no serial lock, so these writes can interleave with the worker's writes (see B-3).

### 1.2 Diagram

```
 Browser (static/app.js + modules/*.js)
   |  REST (fetch, X-Requested-With)        Socket.IO (server -> client only;
   |  MJPEG <img> /api/camera/feed          only inbound handler = 'connect')
   v
+--------------------------------- web_server.py (Flask + Flask-SocketIO, threading) ------------------+
| before_request CSRF check  |  ~172 @app.route handlers  |  _setup_logging (stdout/stderr tee -> file)|
|  enqueue()  ------------------------------------+        |  worker.emit -> socketio.emit (broadcast)  |
|  direct calls: worker.emergency_stop, request_abort, set_staging_roi, confirm_*, load_last_setup   |
|  direct DB: collection_db.get_connection() per request                                             |
+------------------------------------------------|---------------------------------------------------+
                                                 v
         +---------------- web_worker.SortWorker (single thread, Queue(50)) -----------------+
         | states: disconnected -> idle -> sorting <-> paused ; any -> estopped -> (reset)   |
         | _cmd_detect_and_sort (one card)  -- re-enqueues itself when continuous_sorting    |
         |   pick(source) -> drop_on_staging -> move camera over staging -> sleeps/flush     |
         |   -> focus-lock prompt (1st card) -> get_sharp_frame -> save scan jpg             |
         |   -> detect_card_with_corners -> foil pair (+5mm X, 2nd frame)                    |
         |   -> [ID thread: is_card_back, identify_card(hybrid), disambiguate, detect_foil]  |
         |      || pick_from_staging()   -> join                                            |
         |   -> route: SortConfig.get_bin -> low-conf gate -> wishlist -> priority -> overflow|
         |   -> tracker.record_scan (CSV + bins.json + SQLite) -> quick_drop(target_x)       |
         +------|-----------------------------|----------------------------|------------------+
                v                             v                            v
        gcode_control (serial,        web_camera.CameraManager      scan_tracker.ScanTracker
        module globals, no lock)      (device 0, DSHOW, MJPG        -> scan_logs/session_*/
                |                      1920x1080, rotate 90)         -> collection.db (SQLite WAL)
                v                             |
          Marlin (SKR 1.4)            capture thread + watchdog
                                      MJPEG generators per client
```

### 1.3 Worker state machine

`STATES = ('disconnected','idle','sorting','paused','estopped')` (`web_worker.py:31`). The setter (`web_worker.py:202-207`) emits `sorter_state` whenever the state changes. It takes no lock and is written from several threads: the worker, `emergency_stop`, and the serial-error callback at `web_server.py:5453`.

```
disconnected --connect--> idle --start_session--> sorting <--pause/resume--> paused
     ^                     ^  \                     |  \                        |
     |                     |   (calibration, drop   |   bin chain exhausted --->|
  serial error cb          |    tuner, self-test,   |   command exception ----->| (_auto_safety_reset)
  (any state!)             |    test_scan all run   |   serial dropped -------->|
                           |    while state=='idle')|
                        stop_session <--------------+
  any --emergency_stop--> estopped --reset_after_estop--> paused (had session) | idle
  idle/disconnected --resume_stale_session--> paused
```

Observations:
- `idle` is overloaded. Calibration sweeps, new-hardware setup, the drop tuner (with a card held on the head) and test scans all run while the state reads `idle`. Nothing stops a session start from queuing behind them. `_ensure_hardware_ready` checks for a `'calibrating'` state that never exists (`web_server.py:3221`).
- `continuous_sorting` is a separate flag. The loop continues only because every `detect_and_sort` re-enqueues itself at the end (`web_worker.py:2077-2080` and 5 other exits).
- `_abort_requested` is a third, sticky flag. See B-2.

### 1.4 Camera capture pipeline

`CameraManager` (`web_camera.py:33`), with a module singleton `camera = CameraManager()` (`:883`), always device index 0.

1. `_open_capture` (`:185`) tries DSHOW, then ANY, then MSMF. It forces MJPG at 1920x1080 and 30 fps with `BUFFERSIZE=1`, then warms up with 8 reads over up to 2.5 s.
2. `_capture_loop` (`:364`) calls `read()`, rotates 90 degrees clockwise, stores the frame under the lock, then sleeps 30 ms (`:454`). Because `read()` already blocks about 33 ms, the loop actually runs at about 15 fps. If the driver ignores `BUFFERSIZE`, frames queue up in the driver and the "latest" frame lags behind reality.
3. `_watchdog_loop` (`:462`) marks the camera stalled after 5 s and forces a reconnect after 10 s. A reconnect clears `_focus_locked` (`:341`).
4. Consumers:
   - `get_frame()` returns a full copy (about 6 MB per call).
   - `flush_buffer()` waits for N new frames.
   - `get_sharp_frame()` (`:609`) computes Laplacian variance on the full frame until 3 consecutive frames pass 50.
   - `lock_focus()` / `unlock_focus()` set `CAP_PROP_AUTOFOCUS`.
   - `generate_mjpeg` / `generate_mjpeg_with_aruco` JPEG-encode the full 1080x1920 frame once per connected client at 15 and 12 fps.
5. Health listeners (`:87`) feed `web_server._on_camera_health`, which auto-pauses the sort after a 12 s grace period (`web_server.py:5355-5433`).

Frames carry no timestamp or sequence number that a consumer can see, so "a frame captured after the motion finished" cannot be expressed directly. The worker uses fixed sleeps plus `flush_buffer` instead.

### 1.5 Socket events

- Inbound: only `connect` (`web_server.py:4554`). Its replies go out with `socketio.emit`, which broadcasts to every client, not only the one that just connected.
- Outbound: about 50 events, all broadcast.
  - Session: `sorter_state`, `session_started/ended/paused/stats`, `card_detected`, `card_picked_up`, `card_dropped`, `motion_path`, `motion_update`.
  - Bins: `bin_full`, `bin_full_prompt`, `bin_fullness_warning`, `overflow_map_updated`, `bin_emptied`.
  - Prompts: `staging_roi_prompt`, `staging_capture_result`, `focus_confirm_prompt`.
  - E-stop: `estop_triggered`, `estop_reset_complete`.
  - Calibration: `hardware_setup_*`, `probe_result`, `drop_tuner_*`, `self_test_*`, `source_bin_*`.
  - Other: `test_scan_*`, `stale_session_*`, `wishlist_match`, `db_update_progress`, `price_update_progress`, `enrichment_refresh_*`, `camera_health`, `worker_crashed`, `worker_fatal`, `error`, `log_message`.
- Emitted but no client listener (checked with grep over `static/`): `session_paused`, `worker_crashed`, `worker_fatal`, `camera_health`, `estop_reset_complete`, `probe_all_complete`, `test_scan_started`. The auto-pause reasons and worker crashes never reach the UI.
- Listened for but never emitted by this area: `bins_chain_full`, `bin_update`, `staging_capture_prompt`, `bin_empty_check`. These are dead listeners.

### 1.6 REST routes (172 `@app.route`), grouped

| Group | Lines | Notes |
|---|---|---|
| Pages: `/`, `/tome`, `/docs`, `/label`, `/api/openapi.json` | 509-592, 4606 | `/docs` loads Swagger from a CDN. |
| Hardware: connect, disconnect, home, estop, status | 599-639 | Every route except estop is queued. |
| Camera: start, stop, feed, feed-aruco, snapshot, status | 646-696 | `GET /feed` starts the camera as a side effect. |
| Bins: config, locations, machine-positions, test, probe, probe-all, saved-configs CRUD, contents, overflow, card-limit, mark-empty, fullness | 703-952 | saved-configs has a path-traversal hole (S-2). |
| Sort config: modes, configs CRUD, duplicate, validate-query, current | 959-1234 | The preset filename sanitizer is good (`:1003`). |
| Session: start, confirm-staging-capture, staging-snapshot, confirm-focus, set-staging-roi, detect, pause, resume, stop, status, continuous start/stop, undo, wishlist-bin, priority-bin, rehome-interval, scan-images, discard-stale, resume-stale | 1241-1668, 4453-4473 | Pause and stop are queued (see B-9). |
| Test scan | 1448-1460 | |
| Motion and sim | 1675-1763 | |
| Card lookup: autocomplete, overlay-info, printings | 1774-1968 | Linear scans over `CARDS_DATA`. |
| Collection: stats, inventory CRUD, filter, sessions, exports, boxes, dividers, locate, import CSV, cull candidates, wishlist, reset | 1975-2911 | Opens a new SQLite connection per request. |
| Moxfield and integrations | 1493-1649, 4879-5034 | |
| Review queue and detection reviews | 2918-4236 | |
| Database update | 3158-3192 | |
| Calibration: start, cancel, new-hardware-setup, retry, drop-tuner x7, last-setup x6, status, wizard/lock-focus, detect-markers, check-empty, source-bins, offset, max-sweep, generate-markers | 3199-3872 | |
| Prices | 4243-4432 | |
| E-stop reset, source-bin, self-test, support bundle | 4439-4547 | |
| Foil labeling | 4591-4734 | Arbitrary file read (S-2). |
| Enrichment | 4778-4860 | |

`docs/openapi.json` has 161 paths from `tools/generate_openapi.py` and `/api/openapi.json` is generated live. The on-disk copy is slightly stale, which is acceptable because the live endpoint is authoritative.

### 1.7 Config and environment loading

- `config.py` holds only constants and has no environment overrides. It includes the data paths, pHash thresholds, `EXCLUDED_SETS`, and machine constants such as `X_DETECTION_POSITION` and `CAMERA_X_OFFSET`. Both `web_database._step_update_config` (`web_database.py:401`) and `update_all._update_config_bulk_path` (`update_all.py:124`) rewrite `CARDS_JSON_PATH` inside `config.py` with a regex, so tracked source code is edited at runtime.
- `.env` is loaded by `web_server.py:20-24` only. The server reads three variables, and their prefixes are inconsistent: `CARDOMANCER_SECRET_KEY`, `CARDOMANCER_HTTPS`, `CARD_SORTER_NO_AUTO_CONNECT`. `.env.example` documents only the integration and cron variables, none of these three.
- Serial port, camera index, HTTP port 5000 and bind address are hard-coded. The serial port lives in `gcode_control`; the camera index is in `web_camera.py:54`; the port and address are in `web_server.py:5301,5509`.
- The Flask `SECRET_KEY` is persisted to `~/.cardomancer_secret_key` (`web_server.py:308`). Nothing in the app uses Flask sessions, so the key currently protects nothing.

### 1.8 Persistence

| Store | Writer | Notes |
|---|---|---|
| `collection.db` (SQLite, WAL) | `scan_tracker`, `collection_db`, many routes | `get_connection()` runs the full `CREATE TABLE IF NOT EXISTS` `executescript` on every connection (`collection_db.py:32-40`). |
| `enrichment.db` | enrichment scheduler | |
| `scan_logs/session_*/` with `scans.csv`, `session.json`, `bins.json`, `summary.txt`, `scan_images/`, `card_crops/`, `card_crops_b/`, `no_detect/` | `ScanTracker` and worker `_save_*` | `bins.json` is fully rewritten on every scan (`scan_tracker.py:197,412`). Scan images older than the last 2 sessions are deleted at session start (`web_worker.py:2549`). |
| `_last_setup.json`, `setup_<name>.json` | worker `_save_last_setup` and calibration routes | Loaded at boot after `bin_configs/_default.json`, so two sources both write bin positions. |
| `bin_configs/_default.json` plus named configs | `_auto_save_bin_config` (`web_server.py:717`) | |
| `drop_height.json` | `gcode_control.set_drop_offset` | |
| `empty_source_z.json` | worker `:3562` | |
| `staging_roi.json`, `staging_bg_ref.png` | `detection.save_staging_*` | Re-captured at every session start. |
| `foil_labels.json` | label routes | Read-modify-write with no lock. |
| `sort_configs/*.txt` | sort config routes | |
| `logs/card_sorter.log` (50 MB x 5) | `_SafeRotatingFileHandler` | |
| `config.py` | DB updater, `update_all` | Self-modifying; see 1.7. |

---

## 2. Per-file summary

### `web_server.py` (5,514)
The Flask app, all REST routes, the one Socket.IO handler, logging, CSRF, the enrichment scheduler, startup and shutdown.
- `_SafeRotatingFileHandler` (`:69`) and `_setup_logging` (`:138`): a stdout/stderr tee into logging with a recursion guard.
- `_load_or_create_secret_key` (`:308`). `socketio = SocketIO(..., cors_allowed_origins='*')` (`:356`).
- `_csrf_origin_check` (`:404`): accepts `X-Requested-With` or a same-origin Origin/Referer. Socket.IO is exempt.
- Emit wiring (`:451-453`). Enrichment registration (`:461-494`).
- `api_session_start` (`:1241`) runs the preflight checks. It checks v1/v2 hash DBs rather than the v3 DB that production uses (`:1298-1312`; see B-6).
- `_get_card_name_index` (`:1774`), `_build_card_overlay_info` (`:1827`).
- `api_collection_filter` (`:2171`) with `_bulk_fetch_enrichment` (`:2107`).
- Review queue (`:3879-4106`), `_update_scan_csv_row` (`:2999`).
- `_ensure_hardware_ready` (`:3199`), `_wizard_step_status` (`:3653`).
- `_run_price_update` (`:4255`).
- `handle_connect` (`:4554`).
- `_graceful_shutdown` (`:5040`), `_run_health_check` (`:5119`), `_preload_heavy_modules` (`:5216`), `_ensure_single_instance` (`:5260`).
- `main` (`:5297`): installs the camera-health auto-pause, the serial error callback, the bin config and last-setup restore, auto-connect, the stale-session check and the scheduler.

### `web_worker.py` (4,359)
`SortWorker`, the singleton `worker` (`:4359`).
- Core: `enqueue` (`:246`), `request_abort` (`:275`), `emergency_stop` (`:300`), `_run` with crash restart up to 10 (`:369`), `_inner_run_loop` (`:421`), `_auto_safety_reset` (`:456`).
- Manual motion: `_cmd_connect`, `disconnect`, `home`, `move_*`, `test_bin`, `probe_bin`, `probe_all_bins` (`:521-684`).
- Drop tuner, 6 commands (`:702-1032`).
- `_cmd_start_session` (`:1051`) and `_capture_staging_background` (`:1295`). The latter blocks up to 120 s waiting for the user to draw the ROI.
- `_cmd_detect_and_sort` (`:1403-2080`): the per-card hot path. `_process_and_identify` is at `:1622`.
- Continuous mode (`:2084-2105`). `_cmd_undo_last_sort` (`:2109`).
- Image saving (`:2203-2345`), `_capture_foil_pair` (`:2277`), dead `_save_hash_diagnostics` (`:2347`).
- Setup persistence `_save_last_setup` / `load_last_setup` (`:2439-2547`).
- Wishlist and priority (`:2567-2664`). Overflow and fullness (`:2668-2852`).
- `_cmd_pause`, `_cmd_resume`, `_cmd_stop_session`, `_cmd_reset_after_estop` (`:2854-3107`).
- Stale session handling (`:3123-3364`). Self-test (`:3382`). Source-bin count (`:3508-3628`).
- Calibration (`:3632-4046`). `_cmd_test_scan` (`:4050`), which duplicates the cycle.
- Helpers `_emit_session_stats` (`:4295`) and `get_status` (`:4320`).

### `web_camera.py` (883)
`CameraManager`, covered in 1.4. It is solid defensive code, but it assumes a single device and has no per-frame metadata.

### `web_database.py` (441)
`DatabaseUpdater`: Scryfall bulk, then printings map, then image download, then the v1 hash DB (`16bit_rgb_create_card_hashes`), then the v2 hash DB, then a rewrite of `config.py`, then `cards.reload_card_data()`. It does not build `card_hashes_v3.json` or `card_embeddings.npz`, which are what `card_identify_hybrid` actually uses (B-6). `get_current_db_info` `json.load`s both hash DBs on every status call (`:79-89`). It uses `requests`, which is missing from `requirements.txt`.

### `main.py` (347)
The legacy CLI sorter: an OpenCV window, a click-to-set bounding box and the v1 `hashing` pipeline. It does not match production. `identify_card_hash` returns a 2-tuple on the empty path (`main.py:91` `return None, None`) while the caller unpacks three values (`main.py:208`), so it raises `ValueError`. It hard-codes fallback bin 10 and `cv2.VideoCapture(0)`. Candidate for deletion.

### `config.py` (171)
Constants. It carries legacy entries: `HASH_DB_PATH` / `HASH_DB_V2_PATH` (v1/v2), `BOUNDING_BOX_PATH`, `TESSERACT_CMD`, `CAMERA_X_OFFSET` (which duplicates the calibrator's value), and `X_DETECTION_POSITION`. The staging entries (`STAGING_ROI_PATH`, `STAGING_BG_REF_PATH`) become obsolete under the redesign.

### `scan_tracker.py` (531)
`ScanTracker` writes per-session files and SQLite. `record_scan` (`:108`) writes a CSV row with a flush, rewrites the whole `bins.json` (`:197`), and inserts into the DB with a commit. `resume_session` (`:325`) rebuilds counts but leaves the per-bin lists empty, and writes to a `session_..._resumed_N` directory.

### `support_bundle.py` (340)
Builds a zip in memory. Its skip rule looks for `frames/` (`:304`), but the real image directories are `scan_images/`, `card_crops/`, `card_crops_b/` and `no_detect/`, so bundles include every scan JPEG from the last 3 sessions (B-15). `printings_map.json` can exceed 5 MB, and the reader then returns the tail of the file, which is invalid JSON.

### `update_all.py` (426)
The correct offline data pipeline: bulk JSON, printings map, missing PNGs, `build_hash_db_v3.py`, then `build_embedding_db.py`. It uses mtime-based skipping. It is not reachable from the UI.

### `tools/`
- `generate_openapi.py` introspects `app.url_map` with hardware mocks and deep-merges `docs/openapi-overlay.json`. It is fine.
- `memory_profile_session.py` is a tracemalloc soak of `ScanTracker`. It does not exercise the `bins.json` rewrite cost, which is time, not memory.

### `templates/`
- `index.html` is a shell that includes 10 partials.
- `label.html` is the foil-label UI and uses `/api/label/*`.
- `frame_label.html` belongs to `frame_label_tool.py` (a separate server) even though it lives in this app's template folder.
- `tome.html` is the manual placeholder.
- The server also builds HTML by string concatenation for box labels and ArUco markers (`web_server.py:2514`, `:3836`).

### Other files
- `requirements.txt`: Flask and Flask-SocketIO are unpinned. `flask-cors` is listed but unused. `requests` is missing. The optional `ijson` and `pyyaml` are undeclared.
- `package.json`: lint and format only.
- `pytest.ini`: `-m "not live"`.
- `.gitignore`: sound. `.claude/` is ignored, but it currently holds three stale worktrees with full copies of these files, which makes repository-wide grep noisy.

---

## 3. Bugs, races, error-handling gaps, security

Severity: **C** = can damage hardware, cards or data; **H** = breaks a feature silently; **M** = incorrect or fragile; **L** = minor.

### Safety-critical
- **B-1 (C)** `_auto_safety_reset` drives Z down instead of up. `web_worker.py:489` calls `gcode_control.move_z(0)` under the comment "Lift Z to safe height". Z=0 is fully down (`gcode_control.py:55`, where `Z_MAX=220` is the top). This runs after any exception in `detect_and_sort`, `probe_bin`, `test_scan` and similar commands, and it plunges the head into whatever is below. The fix is `z_to_top()` or `Z_CLEAR_HEIGHT`.
- **B-2 (H/C)** The abort flag is sticky. `_abort_requested` is set by e-stop (`:332`), by `/api/calibration/cancel` (`web_server.py:3275`, even when no calibration is running) and by the camera-freeze auto-pause (`web_server.py:5383`). It is cleared only by reset-after-estop, calibration and hardware setup (`:2998, 3654, 3675, 3773, 4018`). `_cmd_resume` and `_cmd_start_session` never clear it.
  - After a camera auto-pause followed by Resume, every `detect_and_sort` returns immediately at `:1453` ("aborted before start"). The loop dies silently.
  - A stray Cancel on the calibration tab makes the next sort session do nothing.
- **B-3 (C)** No serial lock. E-stop writes `M112` from a Flask thread (`web_worker.py:337`) while the worker may be partway through writing a line, and pyserial writes from two threads can interleave. Whether `M112` bypasses the Marlin command queue depends on `EMERGENCY_PARSER`, which is not verifiable from this repo. The same applies to `_graceful_shutdown` (`web_server.py:5058`). Add a write lock in `gcode_control` and give e-stop priority.
- **B-4 (C)** Aborting mid-cycle releases the card at an arbitrary location.
  - After the source pick, the abort branch turns the pumps off at whatever X the head is at (`web_worker.py:1491-1498`), dropping the card there.
  - After the staging drop it returns with the card left on staging (`:1503-1505`). The next session drops a second card on top of it.
  - There is no "safe put-down" routine.
- **B-5 (M)** Starting a session while paused orphans the previous session. `_ensure_hardware_ready` rejects only `sorting` and `calibrating`, so `start_session` from `paused` replaces `self.tracker` without calling `end_session` (`web_worker.py:1255`). The CSV handle leaks and the DB session is left with `end_time NULL`, so it later shows up as stale. Session start (which homes) can also queue behind a running drop tuner that holds a card, because the tuner runs in `idle`.

### Silent functional bugs
- **B-6 (H)** Hash DB generations are out of sync.
  - Production identifies with `card_hashes_v3.json` and `card_embeddings.npz` (`card_identify.py:53`, `card_identify_v2`).
  - The UI "Update database" button (`web_database.py:182-185`) rebuilds only the v1 and v2 DBs, so new sets are never learned from the UI.
  - Session preflight (`web_server.py:1298-1312`) and the startup health check (`:5147`) block or warn on v1/v2 files that the hot path does not use, and never check v3 or the embeddings.
- **B-7 (H)** The wishlist bin never fires. The worker calls `collection_db.check_wishlist_match(conn, card_name, set_code=...)` (`web_worker.py:1898`), but the function signature is `(conn, card_name)` (`collection_db.py:1456`). Every card raises `TypeError`, which is caught and logged as "Wishlist check error".
- **B-8 (H)** The review queue default threshold uses the old 64-bit scale. `/api/collection/review-queue` defaults to `threshold=12.0` (`web_server.py:3883`, and the UI default in `static/modules/review.js:11`). Distances are now on a 256-bit scale (match threshold 120, low-confidence 90; `config.py:29,44`), so nearly every recognized scan lands in the queue.
  - "Confirm" and "Dismiss" work by overwriting `hash_distance = 0` (`:3970, 4101`), which destroys the diagnostic value. Use a `reviewed` column.
- **B-9 (M)** Pause, stop, undo and cancel-calibration are all queued (`web_server.py:1405-1445, 3717`).
  - They wait behind the in-flight card (about 15 s).
  - `/api/session/detect` during continuous mode, and undo while continuous is on (`web_worker.py:2196-2199`), each enqueue an extra `detect_and_sort`. That starts a second self-perpetuating chain, and chains accumulate until the next pause.
  - `_cmd_cancel_calibration` is queued and so only runs after the sweep it is meant to cancel. The API route calls `calibrator.cancel()` directly, so the queued command is dead code.
- **B-10 (M)** Unrecognized cards and card backs route to a hard-coded logical bin 10 (`web_worker.py:1798, 2007`) instead of `sort_config_obj.fallback_bin`. The identity gate already uses `fallback_bin` correctly (`:1884`). Presets with fewer than 10 bins, or a different fallback, get the wrong behavior.
- **B-11 (M)** Undo is inconsistent.
  - It decrements inventory by `(name, set_code)`, ignoring `collector_number` and foil (`web_worker.py:2165-2177`).
  - It leaves the `scan_history` row and the CSV row in place.
  - It mutates `tracker.scan_count` and `bins` directly.
  - `undo_available` survives `stop_session`.
  - `last_scan_id` is never set (dead).
- **B-12 (M)** `_cmd_run_calibration` crashes on a `None` result: `result.get('error')` at `:3715` when `result` is falsy.
- **B-13 (M)** The frame-grab failure path (`web_worker.py:1541-1550`) moves the card to fallback but never records a scan or increments bin counts, so fullness drifts.
- **B-14 (M)** The id thread `join()` has no timeout (`:1779`). A hung DINOv2 or CUDA call wedges the worker forever, because the queue restart logic only catches exceptions.
- **B-15 (M)** Support bundles contain every image of the last 3 sessions, because the skip rule looks for `frames/` (`support_bundle.py:304`). The zip is built in RAM and can reach hundreds of MB.
- **B-16 (M)** Graceful shutdown cannot end the session. The tracker's SQLite connection was created on the worker thread, and `_graceful_shutdown` calls `end_session()` from the main/atexit thread (`web_server.py:5070`). SQLite's default `check_same_thread` raises, the warning is swallowed, and every normal Ctrl+C leaves a "stale session".
- **B-17 (M)** The serial-error callback forces `worker.state = 'disconnected'` from another thread (`web_server.py:5453`). This can override `estopped` and leaves the tracker and continuous loop untouched.
- **B-18 (L)** Resumed sessions write to `session_<ts>_resumed_<id>/`, but `_resolve_session_dir_name` (`web_server.py:2918`) maps a session to `session_<start_time>`. Review-queue crop and scan URLs for resumed segments resolve to nothing, and the resumed tracker's per-bin lists are empty (`scan_tracker.py:355`).
- **B-19 (L)** `api_staging_snapshot` reads `_staging_snapshot_frame`, which is never assigned, so it always returns 404 (`web_server.py:1349`, `web_worker.py:130`).
- **B-20 (L)** `handle_connect` broadcasts `sorter_state` and `stale_session_detected` to all clients on every new connection (`web_server.py:4557-4578`). Use `emit(...)` to the requester only.
- **B-21 (L)** `cards.reload_card_data()` mutates the shared card dicts in place from the DB-updater thread while the worker may be routing (`web_database.py:233`). This is a race during a mid-sort prices refresh.
- **B-22 (L)** Unvalidated `int()` and `float()` on request data (for example `web_server.py:933, 1451, 1655, 3883`) produce 500 errors instead of 400. `request.json` raises 415 when the Content-Type is not JSON in recent Flask versions. `label_set` crashes on a missing body (`:4708`).
- **B-23 (L)** Emitted events have no listener (1.5), so worker crashes and auto-pause reasons are invisible in the UI.

### Security (LAN-only, but worth noting)
- **S-1 (M)** No authentication. The server binds `0.0.0.0:5000` (`web_server.py:5509`) with the Werkzeug dev server (`allow_unsafe_werkzeug=True`). Anyone on the LAN can move motors, run e-stop or reset, call `/api/collection/reset`, or delete sessions.
  - The CSRF check only blocks browser cross-site attacks. Socket.IO is `cors_allowed_origins='*'`, so any web page can open a socket to the kiosk and read all broadcast events.
  - Suggested fix: an opt-in token or PIN, a `CARDOMANCER_BIND` setting (default 127.0.0.1 unless explicitly LAN), and a Socket.IO origin allow-list.
- **S-2 (M)** Path traversal with Windows backslashes. Flask's default `<string>` converter blocks `/` but not `\`, and `os.path.join` on Windows treats `\` as a path separator.
  - `GET /api/label/image/<session_name>/<filename>` (`:4694`) is an arbitrary file read (for example `..\..\..\.env`).
  - `DELETE /api/bins/saved-configs/<filename>` (`:892`) is an arbitrary file delete with no extension check.
  - `GET` on the same route (`:847`) reads arbitrary JSON; `POST` (`:859`) writes `*.json` anywhere.
  - `/api/review/*/<session_name>/...` and `/api/label/cards/<session_name>` are lower-impact variants.
  - Fix: `werkzeug.utils.secure_filename`, or reuse `_safe_preset_filename` together with a resolved-path prefix check.
- **S-3 (L)** Stored XSS. `/api/collection/boxes/labels` interpolates box names into HTML without escaping (`web_server.py:2530-2533`). `frame_label.html` and `label.html` use `innerHTML` with server data.
- **S-4 (L)** `/docs`, the Bootstrap CSS and the fonts load from CDNs, so the kiosk UI depends on internet access for styling.

---

## 4. Optimizations

### 4.1 Per-card hot path (app side)

Timeline of `_cmd_detect_and_sort`, excluding the time the motion commands themselves take:

| Step | Cost | Where |
|---|---|---|
| Settle sleeps after the camera move | 0.5 s + `flush_buffer` (up to 0.5 s) + 0.3 s | `web_worker.py:1509-1511` |
| `get_sharp_frame` | at least 3 frames at about 15 fps, so 0.2 s or more (up to 2 s); full-res Laplacian each iteration | `:1539`, `web_camera.py:609` |
| `_save_scan_image` | synchronous JPEG encode and write of 1080x1920 | `:1555` |
| Foil pair | X move, then 0.25 s + flush + 0.2 s + sharp (up to 1.5 s), then save and warp | `:1598`, `:2303-2311` |
| Identification | pHash plus DINOv2 on every card (both always run; `card_identify_hybrid.py:118-122`), plus disambiguation and `detect_foil` | ID thread |
| Routing | new SQLite connection plus a full schema `executescript` per call (wishlist, priority image lookup) | `:1897`, `:2642` |
| `record_scan` | CSV flush + full `bins.json` rewrite (O(n)) + DB commit | `scan_tracker.py:160-215` |
| `_emit_session_stats` | full per-bin card lists sent over Socket.IO every card (O(n) payload) | `:4313` |
| Continuous delay | 0.5 s default from the API (`web_server.py:1431`) | `:2078` |

That is roughly 2 to 3.5 s of fixed application-side wait per card, on a 15 to 18 s cycle. Recommendations:
1. Add a frame sequence number and timestamp to `CameraManager`, and a `get_frame_after(t, min_sharpness)` method. Then replace the fixed sleeps with "the first sharp frame captured after the motion-complete time". Remove the 30 ms sleep in `_capture_loop` (`web_camera.py:454`), because `read()` already paces the loop. Compute sharpness on a downscaled ROI.
2. Run DINOv2 only when pHash is not confident. Cases 1 to 3 in `card_identify_hybrid.identify_card` do not need the DINO result except for the "both agree" check. Run pHash first, and call DINO only when `p_dist > PHASH_CONFIDENCE_THRESHOLD` and the gap rule fails.
3. Move all image writes (scan, scan_b, crop, crop_b) to a background writer thread or queue.
4. Keep one long-lived collection DB connection on the worker thread for the hot path. Cache the wishlist in memory, as `priority_oracle_ids` already does. Run `_create_tables` once per process, not once per connection.
5. Write `bins.json` only at session end or every N scans. It duplicates the DB and CSV.
6. Emit per-card deltas (`card_dropped` already carries the needed data) and send the full `bin_contents_detail` only on request.
7. Default `continuous_delay` to 0.
8. Make the foil pair optional (config flag), or fold it into the redesign (section 5).

### 4.2 Non-hot-path performance
- MJPEG: every viewer JPEG-encodes the full 1080x1920 frame at 15 fps (`web_camera.py:708`). Encode once per new frame into a shared buffer, downscale for preview (for example 540x960), and skip unchanged frames. This frees CPU for identification.
- `get_frame()` copies 6 MB per call. `get_sharp_frame` calls it at 30 Hz. Return read-only views or copy only the ROI.
- `web_database.get_current_db_info` loads hash DBs on every `/api/database/status` call (`web_database.py:85`). Cache the counts.
- `_run_price_update` re-parses the whole bulk JSON (with no ijson, `json.load` of hundreds of MB; `web_server.py:4314`), even though `cards.CARD_DATA_BY_ID` is already in memory.
- `/api/collection/inventory` and `/filter` load the whole inventory and paginate in Python (`:2016-2022`, `:2208`). Use SQL `LIMIT` and `OFFSET` for the unfiltered case.
- `api_review_lookup` and `_build_card_overlay_info` (name path) scan all of `CARDS_DATA` per request. Reuse `_card_name_index`.

### 4.3 Structure
- **Split `web_server.py`** into Flask Blueprints: `routes/hardware.py`, `camera.py`, `bins.py`, `sort_configs.py`, `session.py`, `collection.py`, `review.py`, `calibration.py`, `enrichment.py`, `integrations_moxfield.py`, `labels.py`, `db_admin.py`. Also split out `app_logging.py` (lines 50-289), `security.py` (secret key and CSRF) and `startup.py` (health check, preload, camera-health auto-pause, shutdown). The route handlers are mostly independent, so this is mechanical and low risk given the existing tests.
- **Split `web_worker.py`** into:
  - `worker/core.py`: queue, state machine, e-stop, crash restart.
  - `worker/cycle.py`: the per-card pipeline as explicit steps.
  - `worker/identify.py`: an identification service that takes an image and returns an `IdResult` dataclass. It replaces the ad-hoc `id_result` dict and the duplicate in `_cmd_test_scan` (`:4191-4217`).
  - `worker/routing.py`: SortConfig, low-confidence gate, wishlist, priority, overflow and fullness.
  - `worker/drop_tuner.py`, `worker/calibration_cmds.py`, `worker/session_persistence.py` (last setup, stale resume) and `worker/image_archive.py`.
- **Formalize the state machine.** Use an explicit enum with busy sub-states (`calibrating`, `tuning`, `self_test`, `test_scan`) and a transition table enforced in one place. Protect it with the existing unused `self._lock` (`:89`). Clear `_abort_requested` at the start of every user-initiated long command.
- **Dead code:**
  - `_save_hash_diagnostics` (`:2347`, unreachable after `return`) and `_resolve_diagnostics_url` / `/api/review/diagnostics-file`.
  - `empty_bin_z`, `last_source_z` and the stack estimate in `_emit_session_stats` (`:101-102, 4305`); these are never set.
  - `get_nearest_source_x` (`:4276`, unused), `last_scan_id`, `_staging_snapshot_frame` and `/api/session/staging-snapshot`.
  - `confirm_staging_capture` (legacy), `_cmd_cancel_calibration`, and the `'calibrating'` check.
  - `main.py`, and the v1/v2 hash paths in `config.py`, `web_database.py` and `hashing.py`.
  - The `/api/sort/modes` legacy route, the `custom_queries` legacy path (both in the worker `:1143` and in the sim `web_server.py:1733`), and the stale `.claude/worktrees/*` copies.
- **Duplicated logic:**
  - Overflow-chain sorting (`web_worker.py:1202-1227` and `:2787-2805`).
  - The pump-release sequence (4 copies in the drop tuner; should be a `gcode_control.release_card()`).
  - Bin config serialization (`_auto_save_bin_config` `:717` and `api_bins_saved_config_save` `:859`).
  - Locate GET and POST (`:2585`, `:2630`) and the review URL resolvers.
  - Identification filtering (`web_worker.py:1664-1671`, `:4199-4207`, `main.py:79-86`).
  - Two sources of bin positions at boot (`_default.json` and `_last_setup.json`).
- **Stop self-editing `config.py`.** Keep a `data/current_bulk.json` pointer, or glob the newest `default-cards-*.json`.
- **Unify the data update path.** Make the UI "Update database" call the `update_all.py` phases, so it produces v3 and the embeddings.

---

## 5. Changes required for the up-camera redesign

The redesign: cards are loaded face-down, picked, imaged from below by a fixed upward camera while held, then placed directly. There is no staging platform.

### 5.1 Camera device management (second camera)
- Replace the single `camera = CameraManager()` (device 0; `web_camera.py:883`) with a `CameraRegistry` of role-named instances. Suggested roles: `up_cam` for identification, and `carriage_cam` (the existing one) kept for ArUco bin calibration and bin-empty checks, or retired.
- Select devices by stable name or serial, not index, because DirectShow indices reorder on replug. Put this in config or env, for example `CARDOMANCER_CAM_UP="USB Camera XYZ"`.
- Make the per-role settings configurable: resolution, rotation or flip (the orientation from below differs from the current 90-degree clockwise rotation), manual exposure and gain, and fixed focus. The geometry is fixed, so focus can be locked once at calibration and persisted, not prompted per session.
- The watchdog and health listener need to know the role. Auto-pause should apply only to `up_cam` during sorting.
- `_focus_locked` must survive a reconnect by re-applying the stored settings instead of resetting (`web_camera.py:341`).
- API changes:
  - `/api/camera/*` gains a `?cam=` parameter.
  - The wizard (`_wizard_step_status`, `web_server.py:3653`) reports each camera.
  - `/api/session/start` checks that `up_cam` is active.
  - Calibration endpoints that pass `camera=camera` must pass the carriage camera explicitly.
- USB bandwidth: two 1080p MJPG streams on one host controller is usually fine, but run the preview only on demand and keep only the up-cam capturing continuously during a sort. Alternatively, use trigger-style capture.

### 5.2 Capture while the card is held
- Add frame metadata (sequence number and monotonic timestamp) and `get_frame_after(t0, min_sharpness, timeout)`, so the worker can request a frame captured after the head reported arrival (`M400` returned), with no fixed sleeps.
- Consider on-the-fly capture: image as the head passes over the camera at a known X, using a short exposure or strobe. This needs a motion-sync hook from `gcode_control` (the motion audit's area), so the application should expose `capture_at(x)` semantics.
- New detection module for the held card: a known dark background or head underside, and a fixed ROI calibrated once. This replaces `detect_card_with_corners` with background subtraction against `staging_bg_ref.png`, and removes `_capture_staging_background` and its 120 s blocking ROI prompt at every session start (`web_worker.py:1102-1112, 1295-1393`).
- Retry semantics change for the better. "No card in frame" now means a failed pick. Re-probe or re-pick at the source immediately, with no staging state to clean up. A blurry frame means re-capture while still holding. Replace `_no_detect_retries` (`:1559-1583`). Add double-feed detection from the image (two edges, or an offset second card).
- Card-back semantics change. With face-down loading, a card back in the image means the card was loaded face-up. Keep `is_card_back` but route to a dedicated "flip me" bin (configurable), not hard-coded bin 10 (B-10).
- Foil pair: the +5 mm X shift over a static card (`_capture_foil_pair`, `:2277`) must become a shift of the held card over a fixed camera. Pixel offset then equals head displacement times the up-cam px/mm. Alternatively, capture two exposures or lighting states. Keep this behind a flag so it does not cost cycle time by default.
- Mirroring: check that the up-cam image of the card face is not mirrored relative to Scryfall references, and that the rotate-180 logic in `identify_card` still covers both loading orientations.

### 5.3 State-machine changes (remove staging)
Remove or replace the following in `_cmd_detect_and_sort`:
- `drop_on_staging` (`:1502`).
- `move_to_camera_position` plus the sleeps (`:1508-1511`).
- The first-card focus prompt (`:1522-1534`), which becomes a calibration-time step.
- `pick_from_staging` (`:1544, 1776`).
- The staging-based `motion_path` payloads (`:1808-1820, 1986-2000, 2027-2041`, which reference `X_STAGING_POSITION`).

Also update:
- `_cmd_test_scan` (`:4050-4274`). Rewrite it on top of the shared cycle rather than keeping a second copy.
- `_cmd_new_hardware_setup`: drop the staging probe (`:3946-3973`) and `expect_staging`. Add an "imaging station" step: head X over the up-cam, card Z height for focus, and px/mm.
- `_save_last_setup` / `load_last_setup` (`'staging'` → `'imaging'`).
- Session preflight (`web_server.py:1281-1294`), which checks `X_STAGING_POSITION` and `X_CAMERA_POSITION`.
- The wizard `staging_roi_set` step (`:3698`).

Remove these endpoints: `/api/session/confirm-staging-capture`, `/staging-snapshot`, `/set-staging-roi`, `/confirm-focus`. Remove these events: `staging_roi_prompt`, `staging_capture_result`, `focus_confirm_prompt`. Remove the config entries `STAGING_ROI_PATH` and `STAGING_BG_REF_PATH`, and the ArUco staging marker (ID 49).

New cycle, sketched as explicit steps:

```
PICK(source) -> MOVE_TO_IMAGER -> CAPTURE (frame after arrival) -> [ID async]
            -> MOVE toward predicted/neutral X while ID runs -> COMMIT bin
            -> PLACE(bin) -> RETURN(source)
   failure edges: no card -> RE-PICK; blurry -> RE-CAPTURE; ID timeout -> fallback bin;
                  abort while holding -> SAFE_PUT_DOWN(fallback or source)   (fixes B-4)
```

### 5.4 Overlapping identification with motion
- There is no staging re-pick to hide the ID latency behind any more (`:1770-1779`). ID now runs while the card is held, and the bin must be known before placing. Options:
  1. Start X travel toward the centroid of likely bins, or toward the fallback bin, and commit or redirect at a checkpoint. This is the speculative-bin-commit plan in `docs/speculative_bin_commit_PLAN.md`. It needs a non-blocking motion API from `gcode_control` (the motion audit).
  2. Make ID fast enough (under 300 ms) that waiting at the imager costs little. Run pHash first and skip DINOv2 when confident (4.1 item 2). Pre-warm CUDA. Downscale before warping.
- `join()` must have a timeout and a fallback route (B-14). Carry the result as a typed `IdResult`.
- Anything not needed to choose the bin moves off the critical path until after the place command is issued: foil detection when the sort config does not use foil, image archiving, DB and CSV writes, and stats emission. Only routing-relevant fields should block. The query parser needs `is_foil` only if the active config references foil, so this can be decided per config.
- Routing lookups must be in-memory: the wishlist cache (B-7 fix) and priority IDs (already cached).

### 5.5 UI and API surface
- New imaging-station calibration and live preview. Per-camera health badges.
- Remove the staging prompts.
- `motion_path` events need the new geometry (imager X instead of staging X).

---

## 6. TODO / FIXME inventory

A grep for `TODO|FIXME|XXX|HACK` finds no real markers in scope. The only hit is the word "XXXX" inside a filename pattern in a docstring (`web_worker.py:2280`). Deferred work is instead recorded in prose comments:

- `web_worker.py:3118-3121`: the stale-session comment says full resume "lands in Phase 4". It has since been implemented at `:3194`, so the comment is stale. The same stale note appears at `web_server.py:4448-4451` and `:5493-5497`.
- `web_worker.py:1143-1145`: "Legacy manual-queries path (kept for API compat until the UI is fully migrated)".
- `web_worker.py:2351-2356`: `_save_hash_diagnostics` is "DEPRECATED… now no-ops".
- `web_server.py:959-966`: `/api/sort/modes` is "Legacy endpoint — kept so any older clients… don't break".
- `web_server.py:1128-1131`: "`?skip_validation=1` (reserved for future UI…)".
- `web_server.py:4820-4823`: "each probe may hit its real endpoint in Phase 0B+".
- `support_bundle.py:66-74`: `_redact_for_bundle` is a "Hook for future redaction" and a no-op today. It will be needed once `.env` gains Moxfield or TCGplayer credentials.
- `.env.example`: Moxfield push, Discord and ntfy notifications, and TCGplayer API are all marked deferred.
- `web_server.py:548-555`: the Tome of Knowledge is a placeholder with "coming soon" sections.
- `PROJECT.md` "Planned" list: frame detection, foil, condition grading, sleeves, ramp overflow bin, and "second camera on the X carriage", which conflicts with the up-camera plan and should be updated.
- `update_all.py:415` points to `test_regression_362.py` as the accuracy check after rebuilding. This is not wired into pytest.
