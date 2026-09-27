# Audit 01: Motion, hardware control, calibration and sort-cycle orchestration

Audit date: 2026-09-27. Branch `docs/audit-and-backlog`. This is a read-only audit: no code was changed.
Line references are to the working tree at the time of the audit.

The main conclusion is that **the 15-18 s cycle comes mostly from Z travel, X travel and fixed sleeps. Slow probing is not the cause.**
`docs/cycle_time_findings_2026-07-19.md` Finding 1 (G38 at 5 mm/s) and Finding 3 (accel 500) were read from the wrong
`Configuration.h`. There are two diverging firmware configs on disk (see section 3.1). The code already sends
`G38.2 ... F3600` (60 mm/s) and already does a cached two-stage descent.

---

## 1. What each file does and how the parts connect

```
browser (motion.js, calibration.js, calibration_wizard.js)
   │  REST + Socket.IO
web_server.py ──enqueue()──► web_worker.SortWorker (single worker thread; queue of _cmd_* handlers)
                                 │            │                     │
                                 │            ├─ web_calibration.BinCalibrator (ArUco sweep, refine)
                                 │            ├─ web_camera.CameraManager (single camera singleton)
                                 │            └─ web_motion_sim.motion_tracker (UI position estimate only)
                                 ▼
                          gcode_control (module-level globals + pyserial) ──USB serial 250000──► Marlin 2.1.3 (BTT SKR 1.4 Turbo)
```

| File | What it does |
|---|---|
| `gcode_control.py` (1355 lines) | The only serial/G-code layer. It holds module-global machine state: feedrates, X positions (source, staging, camera), Z heights, the probe-height cache and bin locations. Low-level I/O is `_send_gcode` (it clamps absolute X/Z moves to a safe envelope, :560-624), `_read_response` (reads until `ok`, :627-654), `_send_and_wait` (:657) and `_get_current_xz` (M114, :679). High-level primitives are `pick_from_position`, `drop_on_staging`, `pick_from_staging`, `quick_drop`, `drop_on_surface`, `move_to_camera_position` and `_probe_with_cache`. There are also legacy/unused primitives: `pick_and_drop`, `send_to_bin`, `deliver_to_bin`, `pick_from_source`, `park_for_camera`, `return_to_detection_nonblocking`, `move_to_detection_position`. Vacuum is `M106 P0` and the release pressure pump is `M106 P1`. `_check_bin_fullness` probe-based bin-full detection runs only in unused primitives. `drop_height.json` persists `Z_DROP_OFFSET`. `connect_to_board()` sends `M211 S0`, which turns off **all** software endstops. |
| `web_worker.py` (4359 lines; motion parts audited) | The single hardware thread. `_cmd_detect_and_sort` (:1403-2080) is the per-card cycle. `_cmd_start_session` (:1051) homes, forces the staging-ROI prompt and loads the sort config. It also contains the E-stop (:300), the auto safety reset (:456), the drop tuner (:702-1032), calibration/new-hardware-setup (:3632-4018), `_cmd_test_scan` (:4050) and setup persistence (`_save_last_setup`/`load_last_setup` :2439-2547). |
| `web_motion_sim.py` | `MotionTracker` is a UI-only linear interpolation of head position. The worker pushes `start_move`/`update_position` to it, so it is not a physics model. `SortSimulator` fakes a sort with random cards. Its step list (pick → move to *detection X* → 0.5 s ID → bin) already matches the planned up-camera flow better than the real code does. |
| `benchmark_motion.py` | A CLI benchmark: X/Z feedrate sweeps with M114 verification, accel-vs-distance, repeatability, a mock sort cycle timed per phase, and M503 parsing (`--info`). This is the right tool to read the *actual* firmware limits. It imports `ser` by value (always `None`, :42), which is harmless but misleading. |
| `web_calibration.py` | `BinCalibrator`. It does an ArUco (DICT_4X4_50) forward sweep with the carriage camera, zero-crossing interpolation, false-positive filters (sample count, spacing, staging exclusion zone), provisional re-verification, iterative visual-servo refinement, `_build_bin_config` (source = 0,-1,-2…; dest = 1..N by X order; staging = marker 49), empty-bin check and nearest-source helper. The camera offset convention is `camera_world_x = carriage_x + CAMERA_X_OFFSET` (default 100, :162-183). |
| `calibrate_staging.py` | Legacy OpenCV-window CLI. It picks a card, lets you jog it over the staging area, probes, then jogs the camera. It **rewrites `gcode_control.py` source with regexes** (:114-131). It prints a claim that the staging Z sets `Z_CLEAR_HEIGHT`, which it does not do. It writes `staging_calibration.json`, which nothing reads. |
| `calibrate_camera_height.py` | Legacy CLI to find a camera Z over staging. It only prints results and saves nothing. |
| `generate_staging_mat.py` | Generates a printable mat with ArUco IDs 100-103 from `DICT_ARUCO_ORIGINAL`. Nothing in the app detects those IDs, so this is dead. |
| `generate_aruco_markers.py` | Generates card-sized DICT_4X4_50 markers (0-9 source, 10-48 dest, 49 staging). The label `BIN {id-9}` (:77) is misleading because bin numbers are assigned by X order, not by ID. |
| `capture_card_back_ref.py` | A CLI that grabs a face-down card from staging (raw `cv2.VideoCapture(0)`, rotate 90° CW) and writes `card_back_reference.png` for `is_card_back`. |
| `card_back_id.py` | A one-off script that counts Scryfall `card_back_id` values from a hard-coded `C:\Users\Jon\Downloads\…json`. It is unrelated to the machine. |
| `static/modules/motion.js` | The canvas side view and top view. It animates `motion_path` waypoints (Z-then-X), draws the staging platform, source and bins, and a camera icon at `detection_x`. It uses hard-coded `X_MAX = 1100` and `Z_MAX = 220`. |
| `static/calibration_wizard.js` | A 7-step wizard: connect, camera, confirm markers, ArUco sweep, probe-all, staging ROI, focus lock. It polls `/api/calibration/status`. |
| `static/modules/calibration.js` | Calibration tab: sweep/cancel, new hardware setup, retry, save/load/delete named setups (`setup_*.json`), the ArUco live feed, camera-offset input and drop tuner buttons. |
| `setup_*.json`, `_last_setup.json` | Saved calibration results. They contain `locations`, `staging{marker_id,x}`, `source_bins`, `probe_results` and `camera_x_offset`. **`camera_x_offset` is 100.0 in every file, so it has never been measured.** `_last_setup.json` equals `setup_bolted bins v1.json` (source X 659.1, staging 550.3, bins 138.7…961.8). |
| `staging_calibration.json` | Output of the legacy CLI (staging X 162). Stale, and never read by the app. |
| `staging_roi.json` | Four pixel corners of the staging platform (portrait 1080×1920 frame). It is re-drawn at **every** session start. |
| `bounding_box.json` | Legacy `detection.py` bounding-box setup (`config.BOUNDING_BOX_PATH`). |
| `bin_configs/*.json` | Named bin layouts (count, spacing, source_x, staging_x, `staging_width` 100). `_default.json` matches the TEST BOARD V2 layout, not the current bolted layout. |

---

## 2. The current per-card cycle, step by step

**Assumptions.** The flashed firmware is `Marlin-2.1.3-b1/Marlin/Configuration*.h` (see section 3.1): X max 350 mm/s, Z max 150 mm/s, accel X 1500 and Z 1000 mm/s², JD 0.08. The layout is `_last_setup.json` ("bolted bins v1"): source 659.1, staging 550.3, camera carriage X = 550.3 − 100 = 450.3, empty source floor Z ≈ 10, staging surface Z 118.3. `Z_CLEAR_HEIGHT` is 200, drop Z = 220 − 50 = 170, and the average source↔bin and staging↔bin distance is ≈300 mm.

**Trapezoid formula used.** t = d/v + v/a when d > v²/a, else 2√(d/a).
- X: 350 mm/s, 1500 mm/s², so the ramp distance is 82 mm and a 300 mm move takes ≈1.09 s.
- Z: 150 mm/s, 1000 mm/s², so the ramp distance is 22.5 mm and a 150 mm move takes ≈1.15 s.

**Host overhead.** Every `_send_and_wait` is one serial round trip, about 5-15 ms including Python. `M400` blocks until motion ends. `_get_current_xz` always burns at least one 0.1 s readline timeout draining (:688-693).

| # | Step | Code | G-code / wait | Est. time |
|---|---|---|---|---|
| 0 | Continuous-loop delay from the previous card | web_worker.py:2078 (`continuous_delay`, UI default 0.5 s, session.js:265) | `time.sleep` | **0.5 s** |
| 0b | Every 100 cards: `G0 Z220`, `G28 X` | web_worker.py:1472-1480 | homing at 50 mm/s from up to 960 mm | ≈20 s per 100 cards, so **≈0.2 s/card** |
| 1 | **Pick from source** `pick_from_position(X_SOURCE_BIN, bounce=retry)` | web_worker.py:1487 → gcode_control.py:1157-1187 | `G90`, `G0 Z200` (no-op), `M400`, `G0 X659` (≈300 mm), `M400` | ≈1.1 s |
| 1a | Cached approach | gcode_control.py:776-781 | `G0 Z{cached+10}` (e.g. 200 → 50 = 150 mm), `M400` | ≈1.15 s |
| 1b | Probe | :784-787 | `G91`, `G38.2 Z-999 F3600` (≈10 mm at 60 mm/s), `M400`, `G90` | ≈0.25 s |
| 1c | Read contact Z | :790 → `_get_current_xz` | 0.1 s drain + `M114` | ≈0.15 s |
| 1d | Vacuum | :1178-1179 | `M106 P0 S255`, `G4 P250` | 0.25 s |
| 1e | (retry only) Z-bounce 3×10 mm at 66 mm/s with `M400` after each | :1119-1154 | 6 moves | ≈1.5 s on retries |
| 1f | Lift | :1186-1187 | `G0 Z200` (150 mm), `M400` | ≈1.15 s |
| | *First card of a session:* the cache is empty, so G38 covers the full 190 mm at 60 mm/s | :776 | | +≈2.5 s once |
| 2 | **Drop on staging** `drop_on_staging(n)` | web_worker.py:1502 → gcode_control.py:1267-1305 | `G0 Z200`, `M400`, `G0 X550` (109 mm), `M400` | ≈0.55 s |
| 2a | Cards 1-3 and every 50th: probe (`_probe_with_cache`); otherwise `G0 Z{cached 118}` (82 mm), `M400` | :1289-1296 | | ≈0.7 s (probe cards ≈1.5 s) |
| 2b | Release | :1299-1302 | `M106 P0 S0`, `M106 P1 S255`, `G4 P500`, `M106 P1 S0` | **0.5 s** |
| 2c | Lift | :1304-1305 | `G0 Z200` (82 mm), `M400` | ≈0.7 s |
| 3 | **Camera over staging** `move_to_camera_position()` | web_worker.py:1508 → gcode_control.py:1248-1257 | `G0 Z200` (no-op), `M400`, `G0 X450` (100 mm), `M400` | ≈0.52 s |
| 3a | Fixed settle | web_worker.py:1509-1511 | `sleep 0.5`, `flush_buffer` (≈0.07 s), `sleep 0.3` | **≈0.87 s** |
| 3b | First card only: focus-lock prompt, blocks up to 60 s for the user | :1522-1534 | | once |
| 3c | `get_sharp_frame(min 50, settle 3)` | :1539 | polls the latest frame every 30 ms | ≈0.1-0.2 s |
| 3d | Save full-res JPEG, then `detect_card_with_corners` | :1555-1558 | | ≈0.1-0.15 s |
| 4 | **Foil pair** `_capture_foil_pair(dx=5)` | web_worker.py:1597-1600 → :2277-2330 | `move_x` → `G0 Z200`, `M400`, `G0 X455.3`; `M400` | ≈0.15 s |
| 4a | Fixed settle | :2306-2308 | `sleep 0.25`, flush, `sleep 0.2` | **≈0.5 s** |
| 4b | Second frame plus JPEG save plus warp | :2310-2318 | | ≈0.2 s |
| 5 | ID thread starts (card-back phash, `identify_card`, disambiguation, foil) | :1622-1773 | runs in parallel with step 6 | 0.3-1+ s (hidden unless longer than step 6) |
| 6 | **Re-pick from staging** `pick_from_staging()` | web_worker.py:1776 → gcode_control.py:1308-1344 | `G0 Z200`, `M400`, `G0 X550` (95 mm), `M400` | ≈0.5 s |
| 6a | Approach | :1326-1331 | `G0 Z128` (72 mm), `M400` | ≈0.63 s |
| 6b | Probe | :1334-1337 | `G91`, `G38.2 Z-999 F3600`, `M400`, `G90` (≈10 mm) | ≈0.25 s |
| 6c | Vacuum | :1340-1341 | `M106 P0 S255`, `G4 P250` | 0.25 s |
| 6d | Lift | :1343-1344 | `G0 Z200` (82 mm), `M400` | ≈0.7 s |
| 7 | `id_thread.join()` | :1779 | | 0 s if ID < step 6 |
| 8 | Routing, DB writes, emits (wishlist check opens a SQLite connection per card, :1896-1901) | :1845-1978 | | ≈0.05-0.1 s |
| 9 | **Drop in bin** `quick_drop(target_x)` | web_worker.py:2003 → gcode_control.py:1214-1236 | `G0 Z200`, `M400`, `G0 X{bin}` (≈284 mm avg), `M400` | ≈1.05 s |
| 9a | Lower | | `G0 Z170` (30 mm), `M400` | ≈0.35 s |
| 9b | Release | | `M106 P0 S0`, `M106 P1 S255`, `G4 P500`, `M106 P1 S0` | **0.5 s** |
| 9c | Lift | | `G0 Z200`, `M400` | ≈0.35 s |
| 10 | Stats emit, re-enqueue | :2066-2080 | | small |

**Totals (steady state):**

| Category | What it covers | Time |
|---|---|---|
| Z travel | ≈700 mm per card. Source 150+150, staging 82+82, re-pick 72+10+82, bin 30+30. Most of it is only because `Z_CLEAR_HEIGHT = 200` sits ~80 mm above the staging surface and ~150 mm above the source stack. | **≈5.6 s** |
| X travel | Four moves: bin→source, source→staging, staging→camera (+5 mm), camera→staging, staging→bin. | **≈3.3 s** |
| Fixed dwells and sleeps | Vacuum 2×0.25, pressure 2×0.5, camera sleeps 0.87+0.45, continuous delay 0.5 | **≈3.3 s** |
| Camera, compute and serial overhead | ≈80 serial round trips, M114 drains, JPEG writes, detection | **≈1.0 s** |
| **Total** | | **≈13-14 s** |

On top of that come first-card and retry costs and G28 amortization. This matches the observed 15-18 s once the extra staging probes, retries and logging are added.

**Implication for the redesign.** Staging drop, camera move, foil shift and re-pick together are ≈6.5-7 s of the cycle. The up-camera removes almost all of it, and lowering the clear height removes most of what is left.

---

## 3. Bugs, risks and code-quality problems

### 3.1 Critical and high

1. **Two diverging firmware configs, neither tracked in the project repo.**
   - `D:\Card_Sorter\Configuration*.h` (root) has:
     - `G38_PROBE_TARGET` disabled (`Configuration_adv.h:2558`)
     - `EXTRUDERS 1` and HIGH endstop states
     - max feedrate {500,500,75} and max accel {500,500,500}
     - `SENSORLESS_HOMING` on
   - `Marlin-2.1.3-b1/Marlin/Configuration*.h` has G38 enabled, `EXTRUDERS 0`, LOW endstops, max feedrate {350,350,150}, max accel {1500,1500,1000} and travel accel 1500.
   - Because the app relies on `G38.2`, the flashed build must be the `Marlin/` copy.
   - `docs/cycle_time_findings_2026-07-19.md` Findings 1 and 3 were read from the root copy, and Finding 1 is wrong anyway, because `G38.2` is sent with an explicit `F3600` (gcode_control.py:785) and Marlin's G38 uses the F word (`G38.cpp:110,119`). `Z_PROBE_FEEDRATE_FAST` is only the fallback when no F is given.
   - `EEPROM_SETTINGS` is enabled (`Marlin/Configuration.h:2441`), so the live values may differ from both copies. Run `python benchmark_motion.py --info` (M503) to get the truth.
   - Neither copy is in git (`git ls-files` shows no `.h`).
   - The comment at `Configuration_adv.h:691` says the pressure pump is on P2_04, but `Configuration.h:1430` sets `FAN1_PIN P2_07`.
2. **`X_FEEDRATE = 27000` (450 mm/s) exceeds the firmware max of 350 mm/s** (gcode_control.py:31). It is silently capped. The "benchmarked max reliable 450" in the comment could not have been measured as 450.
3. **The auto safety reset moves Z to 0, which is fully DOWN.** `web_worker.py:489` calls `gcode_control.move_z(0)` with the comment "Lift Z to safe height". Z homes at the top and Z=0 is fully extended (gcode_control.py:8-10). After any exception in `detect_and_sort`, `probe_bin`, `test_scan` and similar, the head drives down at 150 mm/s into whatever is below it. Software endstops are off, and `clamp_z` allows 0. It should be `move_z(Z_CLEAR_HEIGHT)` or `z_to_top()`.
4. **The E-stop is not an emergency stop, and reset cannot work.**
   - `emergency_stop()` writes `M112` from the HTTP thread (web_worker.py:337) while the worker thread may be inside `readline()` on the same port. That is concurrent pyserial use.
   - `EMERGENCY_PARSER` is disabled (`Marlin/Configuration_adv.h:2684`), so Marlin only parses M112 after the current blocking command (M400, G38, G4, G28) finishes.
   - M112 calls `kill()`, which disables interrupts and loops forever (`MarlinCore.cpp:925-958`). **M999 cannot recover from kill()**, so `_cmd_reset_after_estop` (:2969) sends M999 and re-homes into a dead board. The M106 pump-off lines written after M112 are also never executed.
   - This is the likely root cause of the known "Reset & Re-home doesn't resume" bug.
   - Fixes: enable `EMERGENCY_PARSER`, use `M410` (quickstop) plus `M106 P0 S0` for a soft stop, reserve M112 for a hardware reset path, and add a physical e-stop that cuts motor and pump power.
5. **`M211 S0` disables software endstops on all axes** (gcode_control.py:521; also `web_calibration.py:450` and `web_worker.py:3031`). It is done so that X can pass the firmware's `X_MAX_POS 800`, because the bins reach X 962.
   - As a result, `G38.2 Z-999` (:785, :896, :1335; `calibrate_staging.py:85`) has no lower bound. If the probe switch fails or the wire breaks, Z drives down until it stalls.
   - Fix: raise `X_BED_SIZE`/`X_MAX_POS` in firmware (the rail is about 1100 mm), keep endstops on, and bound G38 targets, e.g. `G38.2 Z{cached-15}` in absolute mode.
6. **Serial desync can make `M400` return early.** `_read_response` gives up after 20 lines without `ok` (≈40 s, :639) and returns silently.
   - The next command's `_send_and_wait` then consumes the stale `ok`, so host sequencing drifts by one reply.
   - Motion order is preserved by the planner, but **`M106` is executed immediately, not in planner order**. After a desync, "vacuum off / pressure on" could fire while Z is still moving, and camera frames could be taken mid-move.
   - Also, `_send_and_wait` ignores write failures: after a serial error it logs "Not connected" and the worker keeps running the rest of the cycle, recording a card as dropped. `detect_and_sort` checks the connection only at entry (:1446).
   - Fix: line numbers and checksums (`N…*cs`) with resend handling, and raise on timeout or disconnect.
7. **Card-back and unknown routing use hard-coded `logical_bin = 10`** (web_worker.py:1798, :2007). This ignores `sort_config.fallback_bin`. On a 7-bin machine the bin-10 lookup falls back to the last dest bin's X, but the tracker records bin 10 and counts or fills a non-existent bin. `_cmd_start_session`'s legacy path (:1147) has the same `bin_count`/10 default.
8. **The no-detect path can leave a card on staging.** When `detect_card_with_corners` fails (:1559-1583), the code assumes staging is empty and picks the next card from source. If a card *is* there but was not segmented (misaligned, double, poor lighting), the next card is placed on top of it. The next re-pick then lifts both, or staging keeps accumulating. This is a moot point after the redesign, but it is a risk today.
9. **Every session start blocks on a manual ROI drawing** (web_worker.py:1110-1112 → `_capture_staging_background` :1295-1379, waits up to 120 s). The wizard (step 6) already saved an ROI. The session-start flow re-prompts every time, and on timeout it continues with a possibly stale background.

### 3.2 Medium

10. **The staging px/mm is wrong by about 2×.** `card_detect.get_staging_px_per_mm()` divides the ROI width in pixels by `gcode_control.STAGING_WIDTH`.
    - `STAGING_WIDTH` defaults to 200 (gcode_control.py:288).
    - It is set to 100 only when a bin_config is loaded, and `load_last_setup` never sets it.
    - `web_calibration` says the platform is 110 mm (:142).
    - So `_capture_foil_pair`'s `dx_px` (web_worker.py:2301) is probably wrong by 1.8-2×, which misaligns the B crop that the foil logic compares.
    - The ROI x-extent of a perspective quad is also not the platform width.
11. **The camera X offset has never been calibrated.** `camera_x_offset` is 100.0 in every setup file. `calibrate_camera_offset()` (web_calibration.py:1795) assumes a 200 mm field of view and is never called. Every bin/source X equals `carriage_centered + offset`, so an offset error is a constant pick/drop X error at *every* bin, while camera-over-staging stays self-consistent.
12. **`_cmd_run_calibration` does not pass `camera_x_offset`** when applying staging (web_worker.py:3689), unlike `_cmd_new_hardware_setup` (:3869). If the offset was changed via the UI, `X_CAMERA_POSITION` uses the stale module value.
13. **Only card count detects a full bin.** `_check_bin_fullness` (gcode_control.py:347) is called only from `pick_and_drop`/`deliver_to_bin`, which are unused. `quick_drop` never probes, so `_on_bin_fullness` (web_worker.py:2832) never fires in production. The autonomy doc says "~300 cards before the Z-probe trips", but there is no probe.
14. **Multi-source bins are dead code.** `get_nearest_source_x` (web_worker.py:4276) is never called. Source bins numbered -1, -2 (web_calibration.py:1658) are stored in `locations` but ignored.
15. **Drop height is one global Z for every bin,** fixed and not stack-aware. An empty bin floor is at Z≈10 and the drop is at Z 170, so the first cards free-fall about 160 mm, which risks flipping or stray landing. Near a full bin the head descends close to the stack, with no probe to catch it.
16. **`get_sharp_frame` counts polls, not new frames** (web_camera.py:609-660). "3 consecutive sharp frames" can be the same frame read three times within 90 ms.
17. **`_auto_safety_reset` and `_emergency_safety_reset` write to serial from other threads** without a lock. The same applies to `emergency_stop`. `gcode_control` has no serial lock at all.
18. **The drop tuner's pickup uses `_probe_with_cache(target_x)`.** The approach can use a dest-bin Z restored from `_last_setup.json` probe_results (web_worker.py:2515-2531). That file could be stale if cards were left in the bin. The approach at `cached+10` would then crash into cards higher than 10 mm above the old reading. The same applies to `_cmd_probe_bin` → `_probe_with_cache`. `start_session` clears the cache, but calibration and tuner commands do not.
19. **Wizard defaults do not fit the current layout.** `max_sweep_x = 790` (calibration_wizard.js:123, calibration.js:9, config.py:161) plus a 100 mm offset means the camera sees at most world X 890. The bolted layout has bins at 961.8, which a default sweep cannot find. The wizard's dest default is 7 while the tab default is 10.
20. **The wizard's ROI step (step 6) does not move the carriage to `X_CAMERA_POSITION` first,** so the user may draw on the wrong view. Step 7 locks focus with no guarantee a card is on staging.
21. **`web_calibration._fine_center_on_marker` has an unreachable branch.** `elif abs(offset_px) > 100` (:1613) comes after `> 50`. The function is legacy and only used by a rare fallback.
22. **`check_bin_empty` moves X without first lifting Z** (web_calibration.py:1710). This is safe only if Z is already high.
23. **`calibrate_staging.py` rewrites Python source** (`update_gcode_control`, :114-131) and prints claims about `Z_CLEAR_HEIGHT`/`Z_STAGING_SURFACE` that do not hold (there is no `Z_STAGING_SURFACE` in gcode_control). This is dangerous and should be deleted.
24. **The simulator's `start_move`** uses `max(dx,dz)/feedrate` with a nominal X feedrate of 450 and ignores acceleration (web_motion_sim.py:68-75). The UI animation and "moving" flags therefore under-estimate real times. This is cosmetic.

### 3.3 Low / code quality

- The module-global mutable state in `gcode_control` (positions, cache, callbacks) is mutated directly from other modules. Examples: `gcode_control._active_bin_locations = …` (web_worker.py:2487, :3681, :3856) and `_probe_z_cache[...] = …` (:2529). There is no Machine/Layout object.
- There are many unused or legacy primitives: `pick_and_drop`, `send_to_bin`, `deliver_to_bin`, `pick_from_source`, `return_to_detection_nonblocking`, `park_for_camera` (no M400 between Z and X, :1243-1244), `move_to_detection_position`, `z_probe_down`, `X_CAMERA_PARK`, `X_DETECTION_POSITION` (also duplicated in `config.py:104`) and `BIN_X_LOCATIONS`. They are used only by legacy `main.py` and `test_10_cards.py`.
- `_save_hash_diagnostics` is dead after its early `return` (web_worker.py:2356).
- `last_source_z` is never set, so the stack-remaining estimate in `_emit_session_stats` (:4305) never shows.
- `pick_from_staging` duplicates `_probe_with_cache` logic inline but skips the M114. That is faster, but it means the staging cache never refreshes after the first three drops.
- `import re` sits inside hot functions (`_clamp_motion_command` :570, `_get_current_xz` :686). `_clamp_motion_command` runs a regex on every G-code line.
- `motion.js` hard-codes `X_MAX = 1100` and `Z_MAX = 220`, and draws the "camera" at `detection_x` (a legacy constant) rather than at `X_CAMERA_POSITION`.
- `benchmark_motion.benchmark_sort_cycle` models a probe as a `G0` to drop Z. It does not match the production cycle and cannot validate cycle changes.
- `card_back_id.py` has a hard-coded personal Downloads path. `generate_staging_mat.py` is unused.
- `web_worker.py` is 4359 lines. The per-card cycle is one 680-line method mixing motion, CV, routing, DB and UI emits. It has no unit tests; only `tests/test_z_bounce.py` touches motion.

---

## 4. Optimizations

Ordered by value per effort for the **current** machine. Most of them carry over to the redesign.

1. **Lower the travel height and make it per-move.** `Z_CLEAR_HEIGHT = 200` is about 80 mm above the staging surface (118) and about 150 mm above the source stack.
   - Measure the tallest bin lip and set clear height to lip + card-hang + ~8 mm (probably ~130).
   - Better: compute the clearance per move as the max lip height between the start and end X.
   - Worth ≈3-4 s per card today, and ≈1.5-2 s after the redesign.
   - The drop height must be ≤ the clear height, so bin drops then need no Z move at all.
2. **Remove fixed sleeps.**
   - Camera settle, currently 0.5 + 0.3 + 0.25 + 0.2 s: use frame-counter-based waits (a new frame whose timestamp is after the move completed), fixed exposure and white balance, and optionally an LED strobe.
   - `continuous_delay` 0.5 s: set to 0.
   - `PRESSURE_ON_MS` 500 and `VACUUM_ON_DELAY_MS` 250: bench-test 100-150 ms and 100 ms. A vacuum sensor would make this closed-loop.
   - Together ≈2-3 s.
3. **Set `X_FEEDRATE` to the real firmware max (350 mm/s)**, or raise `M203`/`M201` after benchmarking with the new X motor. Tune accel before max speed: most moves are ≤300 mm and spend much of their time accelerating. Enable `S_CURVE_ACCELERATION` (it is disabled in both configs) to allow higher accel without skipped steps. `INPUT_SHAPING_X` is also available in 2.1.3 and is worth testing for the long belt.
4. **Remove unnecessary `M400` host round trips.** Marlin executes G0 blocks in order, so a Z block always completes before the following X block starts. An `M400` between them only adds a round trip and stops lookahead; it does not create the "no Z/X overlap" guarantee.
   - The M400s that are truly needed are the ones before `M106`, which is not planner-synchronized, and before camera frames.
   - Enable `LASER_SYNCHRONOUS_M106_M107` in Marlin, which makes M106/M107 planner-synchronous. Whole pick and drop sequences can then be streamed with one M400 at the end, and "ok" flow control keeps the planner full.
   - With `ADVANCED_OK` and `BUFSIZE` 8, several lines can be streamed at once.
5. **Skip the M114 after routine source probes.** `_probe_with_cache` → `_get_current_xz` costs ≥0.1 s drain plus a round trip. Parse the contact position from Marlin's G38 output, or read M114 only every N cards. Card thickness is known, so the cache can be decremented by ~0.3 mm per pick and re-probed each time anyway.
6. **Tighten probe approach margins.** `Z_APPROACH_MARGIN` is 10 mm. With cached contact minus 0.3 mm per card, 3 mm is enough, and G38 at 60 mm/s is already fast. Probe accuracy should be checked at 60 mm/s: at 1000 mm/s² decel, overshoot after trigger is ~1.8 mm, which the cup compliance absorbs.
7. **Remove the foil two-shot motion** (≈0.8 s). Replace it with two LEDs at different angles, fired alternately with no motion. On the up-camera this is a two-frame capture of about 70 ms.
8. **Overlap more work with motion.**
   - Start the X move toward the source *before* the bookkeeping (routing/DB/emit) finishes: queue the next cycle's first moves right after the drop's M400.
   - Write the JPEGs (`_save_scan_image`, crops) on a background thread.
   - The wishlist SQLite connection per card (web_worker.py:1896) should be cached.
9. **Speculative bin commit** (`docs/speculative_bin_commit_PLAN.md`) becomes meaningful only after items 1-4 and the redesign bring the cycle to ≤5 s. In the up-camera design it is simpler: the head leaves the camera toward the hedged bin while ID runs. This requires streaming without M400 so the second G0 blends.
10. **Junction deviation and diagonal moves.** With M400 removed, JD 0.08 → 0.2-0.3 gives smoother Z→X corners. Also consider lifting only to lip clearance vertically, then running diagonal (Z+X) moves above the lips. This breaks the `feedback_no_zx_overlap` convention and needs explicit owner sign-off and a card-slip test.
11. **Speed up homing.** The X homing feedrate is 50 mm/s (`HOMING_FEEDRATE_MM_M`). For the periodic re-home, do `G0 X10` at full speed and then `G28 X`, which saves most of the ≈20 s per 100 cards. Also question whether re-homing every 100 cards is needed once steps are not lost (use M114 against an endstop check).

**Rough projection.**

| Scenario | Projected cycle |
|---|---|
| Current machine with items 1-3 | ≈7-8 s |
| Up-camera redesign plus items 1-6 | ≈4-5 s |
| Plus overlap/speculation | ≈3.5 s |

---

## 5. Impact analysis: the upward-facing camera redesign

New flow: pick the top face-down card from source → travel to the up-camera X → (optionally stop and settle) → capture 1-2 frames of the card face from below → ID in the background while the head heads for the bin → drop.

### 5.1 Code to change

| Area | Change |
|---|---|
| `gcode_control.py` | **Delete** `drop_on_staging`, `pick_from_staging`, `move_to_camera_position`, `_staging_probe_count`/`STAGING_PROBE_INITIAL`/`STAGING_REPROBE_INTERVAL`, `X_STAGING_POSITION`, `X_CAMERA_POSITION`, `Z_CAMERA_POSITION`, `STAGING_WIDTH`, `_camera_x_offset` (if the down camera goes away) and the legacy primitives listed in section 3.3. **Add** `X_UPCAM`, `Z_UPCAM_IMAGE` (card plane height over the lens) and `present_to_upcam(settle=…)`, which moves to the camera X at imaging Z with a single sync point for the capture. Add an optional `present_to_upcam_offset(dx)` for the two-shot variant. Replace `set_machine_positions(staging_x, camera_x_offset, staging_width)` with `upcam_x`/`upcam_z`. `quick_drop` stays. |
| `web_worker._cmd_detect_and_sort` | Rewrite as pick → present → capture → ID thread → (hedge move) → join → route → drop. **Every failure path changes because the card is now on the head, not on a platform:** no-detect, frame failure and card-back must drop the held card into a reject bin or back into the source (a new "reject" role, not hard-coded bin 10). The no-detect retry counter's meaning changes. Remove `_capture_foil_pair`/`_flush_foil_b_crop` or rework them for the up-camera. |
| `web_worker._cmd_start_session` | Remove `_capture_staging_background` (the ROI prompt), or replace it with an automatic up-camera background capture with the head parked away (no user click). Rework the first-card focus-lock prompt to "hold card over camera". |
| `web_worker._cmd_test_scan` (:4050) | Rewrite on the same primitives. It currently duplicates the whole staging flow. |
| `web_worker` setup persistence (`_save_last_setup`/`load_last_setup` :2439-2547) | Schema v2: `upcam{x, z_image, px_per_mm, rotation, flip, center_px}` instead of `staging`. Migration: ignore `staging` and `probe_results.staging` in v1 files. `setup_*.json`, `_last_setup.json` and `bin_configs/*.json` (`staging_x`, `staging_width`, `detection_x` keys) all change. |
| `web_calibration.py` | Remove `STAGING_MARKER_ID` 49 handling, the staging exclusion zone and `STAGING_REFINE_PARAMS`, or repurpose marker 49 as an "up-camera location" marker. Add a new up-camera calibration routine (section 5.3). The ArUco bin sweep stays only if the down camera stays. |
| `web_camera.py` | Currently one global `camera = CameraManager()` on device 0, rotated 90° CW. It needs **two instances** with per-device index, rotation/flip, resolution and exposure configuration. The up-camera needs manual exposure, white balance and focus locked, and ideally a trigger or strobe. Every `from web_camera import camera` in web_worker (7 places) and web_server needs the right instance. MJPEG endpoints need a camera selector. |
| `card_detect.py` / `detection.py` | Staging ROI and background subtraction (`save_staging_bg`, `staging_roi.json`, `detect_card_on_staging`, `get_staging_px_per_mm`) no longer apply. The card is now a bright rectangle against the dark underside of the head, at a nearly fixed position, so a fixed crop window plus corner refinement is enough. The card's pose relative to the cup varies by a few mm per pick, so corner detection is still needed. |
| `card_identify*` `is_card_back` | Repurpose: with face-down loading, a card-back seen by the up-camera means the card was loaded face-up. Route it to a reject/"flip me" bin and keep `card_back_reference.png`, re-captured with the up-camera and its lighting. |
| Foil detection (`foil_detect.py`, `plans/foil_detection_plan.md`) | The current method depends on two views from a 5 mm carriage shift over a stationary card. Options: (a) shift the held card 5 mm over the up-camera (same geometry, but the dx sign flips and px/mm comes from up-camera calibration); (b) two LEDs at different angles with no motion (preferred); (c) drop the pair and use single-frame foil cues. This is an owner decision. |
| `web_server.py` | Remove or replace `/api/session/set-staging-roi`, `/confirm-staging-capture`, `/staging-snapshot` (:1339-1377), the `staging_x`/`staging_width` machine-position fields (:712-798, :883, :4750) and the session-start preflight "no_staging_configured" (:1281-1290). Change the calibration status `wizard.staging_roi_set` (:3635-3715) to `upcam_calibrated`. |
| `static/modules/motion.js` | Remove the staging platform drawing and draw the up-camera at `upcam_x`. `motion_path` waypoints emitted from the worker (web_worker.py:1808-2041) currently start at `staging_x` and must start at `upcam_x`. |
| `static/calibration_wizard.js` | Step 3 text (marker 49), step 4 "expect staging" checkbox, step 6 (ROI) becomes "Calibrate up-camera", and step 7 focus lock uses the up-camera with the calibration card held. |
| `static/modules/calibration.js` | Remove `cal-expect-staging` and add up-camera calibration controls. Setup summaries mention staging (:192, :276). |
| Templates | `_tab_sort.html` shows the live staging view; `_tab_setup.html` has staging controls. `plans/sort_flow_stages.md` says "Live camera view of the staging platform". |
| `web_motion_sim.py` | Already models pick → detection X → bin. Point `detect_x` at `upcam_x` and drop `X_STAGING_POSITION`. |
| `benchmark_motion.py` | Rewrite `benchmark_sort_cycle` phases to pick → present → bin, with real G38 probing and the true dwell values. |
| Tests | `tests/test_calibration_status.py` (staging_roi_set), `tests/test_z_bounce.py` (pick primitive), `test_staging_detection.py`, `test_detect_only.py` and the diag scripts that use staging functions. |
| Docs | `docs/cycle_time_findings…` (correct Findings 1 and 3), `plans/z_bounce_retry_plan.md` (retry semantics change), `plans/autonomy_ladder.md` (no-detect retry), `plans/sort_flow_stages.md`. |

### 5.2 Delete or archive

`calibrate_staging.py`, `calibrate_camera_height.py`, `generate_staging_mat.py`, `staging_calibration.json`, `staging_roi.json`, `staging_bg_ref.png`, `bounding_box.json` (if `detection.setup_bounding_box` goes too), `card_back_id.py` (unrelated one-off), the legacy gcode primitives in section 3.3, and marker 49 from `generate_aruco_markers.py` (unless repurposed). `capture_card_back_ref.py` should be rewritten against the up-camera through the web app rather than raw `VideoCapture(0)`.

### 5.3 New things needed

1. **Up-camera mounting and optics.**
   - Choose a lens with a card-sized FOV (about 70×95 mm plus margin) at the imaging distance.
   - Use fixed focus. With a large aperture the DoF must cover card bow (±2 mm) and the per-pick Z variance.
   - Add a cover glass, because the lens faces up under falling dust and dropped cards, and plan for cleaning.
   - A global-shutter sensor is preferable if capture on the fly is ever wanted.
2. **Lighting.**
   - Use a diffuse ring or dome around the lens. Glossy card faces reflect a ring light directly back (hot spots), so use cross-polarization (polarizer on the light and a crossed analyzer on the lens) or low-angle side lights.
   - Card edges need contrast against the head: make the underside of the head or carriage matte black, or paint a dark backdrop plate behind the cup that is smaller than the card.
   - Ambient light from above is blocked by the card itself, which helps consistency.
3. **Up-camera calibration routine (replaces the staging ROI and camera-offset steps).**
   - Make a printed calibration card with ArUco/ChArUco and card-size outline marks. The head picks it from the source bin or a dedicated slot and holds it over the up-camera.
   - From one frame, compute:
     - **camera X** in machine coordinates, by servoing X until the card center is at the image center (reuse the `_refine_bin_positions` slope method)
     - **px/mm** at the card plane
     - **rotation** of the card axis relative to the image axes
     - **flip/mirror flags**
     - **cup center versus optical axis**
     - the **expected card quad** for a fixed crop window
   - Optionally iterate Z to find best focus and store `Z_UPCAM_IMAGE`.
4. **Image geometry and orientation.**
   - A camera looking at the face side of a card sees it **un-mirrored**. Mirroring only appears with a mirror or prism in the path, or through driver or axis conventions.
   - What changes is the mapping from machine axes to image axes, and the rotation of the card relative to the image. The orientation of face-down cards in the source (top edge toward +X or −X) decides whether the image is 0° or 180°. `identify_card` already tries both.
   - The carriage-X-to-image direction is reversed compared with the down camera, which affects the foil `dx_px` sign and any servoing.
   - Store `rotate`/`flip` per camera in config rather than the hard-coded `ROTATE_90_CLOCKWISE` (web_camera.py:54).
5. **Card held by suction.**
   - The card sags and bows around a single central cup. Edges may droop 1-3 mm, which gives slight trapezoid and focus falloff.
   - The card swings or vibrates after X deceleration (belt gantry), so a settle time or short exposure plus strobe is needed.
   - The card is off-center relative to the cup by a few mm per pick, so corner detection is still needed.
   - The card could rotate on the cup during the high-accel X moves that follow. Slip tests are needed at the target accel.
   - Face-down loading means the cup is on the back and the face is fully visible. This is a big improvement over staging.
6. **Double-pick detection (new risk).** Today a two-card pick yields an oversized blob on staging, which is detected as a no-detect. With the up-camera, the camera sees the **bottom** card's face. Both cards are then routed by the bottom card's identity, so the top card is silently mis-sorted. Options:
   - a vacuum or pressure sensor
   - thickness measured from the Z contact positions (two picks versus one stack drop)
   - a side-view or edge camera
   - card weight
   - comparing the stack-top Z drop between consecutive picks (≈0.6 mm instead of 0.3 mm), which is cheap and already available from `_probe_z_cache`
7. **Reject / flip bin.** A configurable physical bin for held cards that cannot be identified: no card detected on the head, card back seen, or double-pick suspected. This replaces the hard-coded `logical_bin = 10`.
8. **Source-bin emptiness.** The current method (probe count, or the ArUco floor marker via the down camera) still works. With the up-camera, an empty pick shows no card over the camera, so the "3 no-detects → stop" rule maps to "3 empty picks → source empty" and needs no staging.
9. **Z-bounce retry.** Keep it. It is now triggered by a double-pick signal or an "empty head" frame instead of a staging no-detect.

### 5.4 What stays

Bin-X ArUco calibration (if the carriage camera is kept), the drop tuner, the probe cache for the source, `quick_drop`, overflow and fullness logic, E-stop plumbing (after fixing it), and the sort routing path.

### 5.5 Open questions for the owner

1. Is the down-looking carriage camera **kept** (ArUco bin sweep, empty-bin checks), or removed with bins calibrated another way (jog-and-save, or up-camera plus a probe)? If it is removed, the camera-offset concept disappears.
2. Where does the up-camera sit on X? Between source and bins, adjacent to the source, would minimize travel. Is there vertical space under the head at clear height for the lens and focus distance?
3. Does the head **stop** over the camera, or is capture done on the fly with a strobe and global shutter? This decides the settle budget.
4. Foil: two-position shots, two-LED photometric capture, or drop foil detection from the routing-critical path?
5. Which physical bin is the reject/flip bin?
6. Which double-pick detection method is preferred (see 5.3.6)? Is a vacuum pressure sensor on the SKR (spare thermistor or endstop input) acceptable?
7. Is `feedback_no_zx_overlap` a hard rule (card slip) or can diagonal moves above bin lips be tested?
8. Which firmware is flashed, and can it be re-flashed with: `X_MAX_POS` ≈1100 so soft endstops can stay on, `EMERGENCY_PARSER`, `LASER_SYNCHRONOUS_M106_M107`, `S_CURVE_ACCELERATION`, `INPUT_SHAPING_X`, `ADVANCED_OK`? Should the config files be tracked in git (e.g. `firmware/` in this repo)?
9. What is the exact bin-lip height, so the new clear height can be set?
10. Face-down loading orientation: are cards loaded with a consistent top edge? This removes the 180° double ID.

---

## 6. TODOs and loose ends found in code and docs

- `static/modules/calibration.js:85-86`: "A future polish pass should gate [the ArUco MJPEG stream] on the calibration sub-section actually being scrolled into view."
- `web_calibration.py:276-282`: if bit-flip misreads persist, switch to `DICT_5X5_100`/`DICT_6X6_250` (requires reprinting markers).
- `web_worker.py:2351-2356`: `_save_hash_diagnostics` is marked DEPRECATED and is a no-op with dead code after `return`.
- `web_worker.py:3118-3121`: full power-loss resume "lands in Phase 4". It is partly implemented at :3195.
- `plans/sort_flow_stages.md`: open bug "Reset & Re-home doesn't actually resume cleanly" (root cause identified in 3.1 #4). Also still to do: source-bin count from probe height, power-loss limbo state, and the Phase 4 Sort tab rebuild.
- `plans/z_bounce_retry_plan.md`: "Step 4 — Manual validation (human)" is still to be done (a live stuck-pair test). Out-of-scope idea: vacuum-pulse release.
- `docs/cycle_time_findings_2026-07-19.md`:
  - It proposes two-stage descent (already implemented via `_probe_with_cache`) and an M400 audit (done here: M400 after nearly every move).
  - S-curve and accel are gated on the new X motor (1.5-1.7 A, "on order").
  - The JD slip test is still to do.
  - Findings 1 and 3 need correcting (section 3.1 #1).
- `docs/speculative_bin_commit_PLAN.md`: gated on cycle < 4 s. Phase 1 instrumentation (per-card timestamps) is worth doing now, because it is also the measurement harness this audit lacks.
- `plans/autonomy_ladder.md`: "~300 cards per bin before the Z-probe fullness check trips". No production probe fullness check exists (3.2 #13).
- `generate_aruco_markers.py:77` label numbering does not match bin numbering (cosmetic).
