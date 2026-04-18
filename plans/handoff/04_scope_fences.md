# Scope Fences

What implementation agents must NOT touch. These are hard boundaries — if a feature seems to require modifying a fenced file, stop and ask the user.

---

## Strictly off-limits (do not modify)

### Detection & hashing pipeline
- `detection.py` — live card detection via contour/hash matching
- `hashing.py` — phash generation, hash DB lookup
- `card_detect.py`, `card_identify.py`, `card_identify_hybrid.py`, `card_identify_v2.py`
- `card_lookup.py`
- `build_hash_db_v3.py`, `create_card_hashes*.py`, `rgb_create_card_hashes.py`
- `card_hashes*.json`, `card_hashes_packed.npz`, `card_embeddings.npz`
- `frame_signatures.json`, `layout_signatures.json`, `layout_signatures.py`
- `find_frame_signatures.py`, `smaller_art_hashing_rgb.py`

**Why:** hash DB is keyed on `CROP_SIZE = 745` to match Scryfall PNGs; any detection change risks invalidating the entire hash database. Detection is working and user has locked-in calibrated behavior.

### Motion / gcode / hardware control
- `gcode_control.py` — Marlin serial protocol
- `benchmark_motion.py`, `web_motion_sim.py` (simulator, not preview UI)
- Any motion timing logic inside `web_worker.py`

**Why:** user has open memory `feedback_no_zx_overlap.md` — Z must complete before X starts. Changes here risk crashing the machine.

### Camera
- `web_camera.py` — camera reader, mjpeg stream, health monitoring
- `staging_*` files (calibration references)
- `capture_card_back_ref.py`, `card_back_reference.png`, `card_back_id.py`

**Why:** camera pipeline is tuned for the current ROI/background setup; changes risk breaking detection.

### Calibration (logic, not UI)
- `web_calibration.py` — the ArUco sweep, source bin detection, offset computation
- `generate_aruco_markers.py`, `aruco_markers/`
- `calibrate_camera_height.py`, `calibrate_staging.py`, `generate_staging_mat.py`

**Why:** calibration math is correct; wizard wrapper should invoke existing endpoints, not re-implement.

### Config
- `config.py` — hashing constants (CROP_SIZE, thresholds), ART_REGION variants, ArUco ID ranges

**Exception:** you MAY add new enrichment-related constants at the bottom of `config.py`, clearly separated with a `# --- Enrichment ---` section header. Do not modify any existing value.

### Firmware / hardware configuration
- Any `*.json` setup files (`setup_TEST BOARD*.json`, `_last_setup.json`, `staging_calibration.json`, `bounding_box.json`)

**Why:** user memory `project_firmware_config.md` — working firmware settings are delicate.

### Card data pipelines (raw)
- `download_cards.py`, `download_missing_frames.py`
- `AtomicCards.json`, `default-cards-*.json`, `downloaded_cards/`
- `printings_map.json`, `missing_frame_printings.json`
- `update_all.py`, `apply_verified_corrections.py`, `backfill_csv_from_db.py`

**Exception:** you may READ these; you may not modify the files or their generating scripts. Enrichment pulls are ADDITIVE — they live in new files.

### OCR
- `ocr.py`, `test_ocr*.py`

**Why:** user memory `feedback_ocr_not_reliable.md` — OCR is not primary detection; don't expand its role.

---

## Modify with caution

### `web_server.py`
- OK to add new endpoints following existing conventions.
- OK to register new SocketIO event handlers.
- OK to wire new modules (e.g., enrichment manager) via the same `set_emit()` callback pattern used for `worker`, `db_updater`, `calibrator`.
- **Do not** restructure the existing endpoint order or grouping without reason — keeps diffs readable.
- **Do not** modify existing endpoints' request/response shapes. If a change is needed, add a new endpoint and deprecate the old one later.

### `web_worker.py`
- OK to add hooks for storage plan tracking (emit `storage_divider_advanced` when a bin fills).
- OK to add a hook for emitting `card_detected` with enrichment data.
- **Do not** change the queue / state machine logic.
- **Do not** change anything related to serial commands or motion sequencing.

### `web_database.py`
- OK to add functions for triggering enrichment refresh.
- **Do not** modify the card-DB build pipeline.

### `collection_db.py`
- OK to add new tables (`storage_locations`, `storage_sessions`, `sync_manifests`) following the existing `_create_tables` pattern.
- OK to add new helper functions.
- **Do not** modify existing tables' schemas without a migration that preserves data.
- **Do not** change the `WAL` mode or foreign-key settings.

### `static/app.js`
- OK to add new view logic, new SocketIO handlers, new filter/sidebar code.
- **Do not** restructure the existing tab navigation (beyond the tab consolidation that removes Motion/Calibration/Database as tabs).
- **Do not** change existing SocketIO event names or payloads.

### `templates/index.html`
- OK to add new tab content, new modals, new drawers.
- OK to remove Motion Preview / Calibration / Database tabs (they become modals/subsections).
- **Do not** change the navbar or E-stop button structure.

### `static/style.css`
- OK to add new classes for new views.
- **Do not** change existing CSS variables or the `[data-theme="light"]` override block.
- New classes should use existing CSS variables for colors; don't hardcode.

### `query_parser.py`
- OK to add new tokens: `staple:`, `salt>`, `salt<`, `combo:`, `cedh:`, etc.
- **Do not** change the behavior of existing tokens (`otag:`, `atag:`, `c:`, `cmc:`, `usd:`, etc.).

---

## Free to create (new files)

- `web_enrichment.py` — enrichment manager (pulls from sources, writes to enrichment.db)
- `web_integrations.py` — Moxfield / Spellbook / edhtop16 client wrappers
- `web_locator.py` — physical locator logic
- `web_storage.py` — box/divider tracking
- `enrichment_db.py` — enrichment.db access layer (mirrors `collection_db.py` style)
- `probes/` directory with one file per source
- `tests/enrichment/`, `tests/probes/`, `tests/fixtures/`
- `tests/probe_snapshots/`
- `sort_configs/` — new preset files
- `plans/` — more planning docs if needed

---

## Files the agent should NEVER create

- New hash databases
- New detection or hashing modules
- Anything named `detection_*`, `hashing_*`, `card_identify_*` — existing pipeline is complete
- Git-ignored secrets files outside `.env`
- Any executable that modifies motion or hardware state without going through `web_worker`

---

## Ask the user before...

- Adding or changing any dependency in `requirements.txt` beyond the pinned list in `05_dependency_pins.md`
- Changing any port, database path, or file location
- Deleting any file, even one that appears unused (there's a lot of one-off scripts)
- Any operation that writes outside the project directory
- Any network request to a host not listed in `web_enrichment_source_probes.md`
- Any change that touches more than 3 existing files in a single edit

---

## Rule of thumb

**If in doubt, make it additive.** New file, new function, new endpoint — all preferred over modifying existing code. The sorter works today; don't break it to add features.
