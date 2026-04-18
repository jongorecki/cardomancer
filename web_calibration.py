# web_calibration.py
# ---------------------------------------------------------------------------
# ArUco marker-based automatic bin calibration.
#
# Uses ArUco markers placed in the bottom of each bin. The camera, mounted
# to the X carriage at a fixed offset from the suction head, sweeps across
# the X axis detecting markers. When a marker is found, the carriage fine-
# centers the marker in frame and records the bin's X position.
#
# Marker ID ranges:
#   0-9   : Source bins (the "to be sorted" stack)
#   10-48 : Destination bins (sort targets)
#   49    : Staging platform (white background detection surface)
#
# Empty bin detection:
#   If a marker is visible in a bin, the bin is empty (cards cover the marker).
#   This works because markers are placed on the bin floor, underneath the cards.
#
# Dual-source optimization:
#   When multiple source bins are detected, the sort cycle picks from
#   whichever source bin is closest to the carriage's current position.
# ---------------------------------------------------------------------------

import cv2
import numpy as np
import time
import threading

from config import (
    CAMERA_X_OFFSET as CONFIG_CAMERA_X_OFFSET,
    CALIBRATION_MAX_SWEEP_X as CONFIG_MAX_SWEEP_X,
)

# ArUco configuration
ARUCO_DICT_TYPE = cv2.aruco.DICT_4X4_50
MARKER_SIZE_MM = 40  # Physical marker size (mm), for reference

# Marker ID ranges
SOURCE_MARKER_IDS = set(range(0, 10))      # IDs 0-9 = source bins
DEST_MARKER_IDS = set(range(10, 49))       # IDs 10-48 = destination bins
STAGING_MARKER_ID = 49                      # ID 49 = staging platform

# Sweep parameters — defaults loaded from config.py
DEFAULT_MAX_SWEEP_X = CONFIG_MAX_SWEEP_X   # Maximum X distance to sweep (mm)
SWEEP_FEEDRATE = 3000           # Sweep speed (mm/min) ~50mm/s
SWEEP_STEP_SIZE = 10.0          # X increment per detection frame (mm)
# Tolerance for the refinement pass. 5px is ~0.15-0.25mm depending on
# camera-to-bin distance, which is well below the mechanical tolerance
# of the carriage so it's as tight as we can meaningfully aim for.
FINE_CENTER_TOLERANCE = 5       # Pixels from frame center to accept as centered
FINE_CENTER_STEP = 0.5          # mm to nudge during fine-centering (was 2.0)
FINE_CENTER_MAX_ITERATIONS = 40 # Max attempts to center a marker

# --- False-positive suppression ---
#
# ArUco detection is fast but not perfect. Common failure modes we see in
# practice:
#   1. ID misread: a marker with ID N gets reported as ID M because one or
#      two bits of the binary pattern are misclassified (e.g. 16 -> 18).
#      Usually a single-frame blip while the real marker is locked correctly
#      from a neighboring camera position.
#   2. Ghost detection: reflections, specular highlights on the bin floor,
#      or adaptive threshold artifacts produce a single-frame "marker" at
#      a plausible ID. Again, a one-frame wonder.
#   3. Phantom near a real marker: the real marker is locked correctly, but
#      1-2 frames around it also report a spurious different ID nearby.
#
# Defenses:
#   A) Require a minimum number of observations before accepting a marker.
#      Real markers are visible for many consecutive frames as the camera
#      crosses them (~10 frames at 10mm steps with a typical FOV). A marker
#      seen in only 1-2 frames is overwhelmingly likely to be a false
#      positive.
#   B) Enforce a minimum physical spacing between locked markers. Bins are
#      physically at least ~50mm apart in the current setup, so no two real
#      markers can be closer than that. When two candidates are within the
#      minimum spacing, keep the one with more observations.
MIN_MARKER_SAMPLES = 3           # Minimum frames to accept a marker lock
MIN_BIN_SPACING_MM = 50.0        # Minimum world-X distance between locked
                                 # markers. Candidates closer than this
                                 # collapse to whichever has more samples.

# --- Provisional marker re-verification ---
#
# A marker seen in only 1-2 sweep frames fails the MIN_MARKER_SAMPLES filter,
# but that's not always a false positive. Real markers can slip through with
# a low sample count when:
#   - the camera's 10mm step landed just off-center twice in a row and the
#     marker left the frame before we got a third hit
#   - detector hardening rejected borderline-valid decodes for a few frames
#   - shutter/USB timing dropped a frame right as we were crossing the marker
#
# Rather than throwing these out, we remember them as "provisional" and,
# during the refinement pass, physically drive back to each provisional
# marker's interpolated X and take a long burst of frames at that stable
# position. If the marker actually shows up in enough of those frames it
# gets promoted to discovered_bins and refined like a normal marker.
# Real bit-flips / reflection ghosts will fail to reappear and be rejected.
# --- Staging platform exclusion zone ---
#
# The staging platform is a physical surface 110mm wide. Any marker detected
# within the platform's footprint (other than the staging marker itself) is
# a false positive — likely a bit-flip or reflection from the staging marker.
# We reject any non-staging marker whose world X falls within
# STAGING_PLATFORM_WIDTH_MM / 2 of the staging marker's detected position.
STAGING_PLATFORM_WIDTH_MM = 110.0

PROVISIONAL_VERIFY_FRAMES = 15   # Frames to sample during re-verification
PROVISIONAL_VERIFY_MIN_HITS = 8  # Required hits out of VERIFY_FRAMES
# After driving to a provisional marker we have to wait for the USB
# camera's autoexposure / autowhite balance to recover from the motion
# blur it just saw, otherwise the first several frames come back
# washed-out or dark and the ArUco detector can't decode the marker.
# The in-sweep detector doesn't care because it's already in steady
# state at every step, but the verification pass makes large jumps
# between markers and needs the extra time.
PROVISIONAL_VERIFY_SETTLE_SEC = 1.5   # Seconds to wait after arriving
PROVISIONAL_VERIFY_WARMUP_FRAMES = 5  # Frames to discard before counting

# Post-sweep interpolation: a marker whose pixel offset goes from +px to
# -px between two neighbouring steps crossed frame-center in between.
# We approximate its true position by linearly interpolating the carriage
# X position where offset = 0. This lifts the sweep resolution well below
# the physical step size without any extra motion.

# Camera X offset convention (IMPORTANT):
#
#   CAMERA_X_OFFSET = camera_world_x - head_world_x
#
# That is, the signed distance from the suction head to the camera, measured
# along the machine's X axis.
#   - POSITIVE value = camera is to the RIGHT of the head (higher X)
#   - NEGATIVE value = camera is to the LEFT of the head (lower X)
#
# Marlin's G0 X command controls the head position. At any carriage
# coordinate `C`:
#     head   world X = C
#     camera world X = C + CAMERA_X_OFFSET
#
# Derived identities used throughout this file:
#     camera_centered_on(world_pos)   => carriage_x = world_pos - CAMERA_X_OFFSET
#     head_over(world_pos)            => carriage_x = world_pos
#     sweep centers at `centered_x`:    marker_world_x = centered_x + CAMERA_X_OFFSET
#
# Single source of truth is config.CAMERA_X_OFFSET — adjust there or via the
# /api/calibration/offset endpoint (which the Calibration tab hooks up to).
CAMERA_X_OFFSET = CONFIG_CAMERA_X_OFFSET


class BinCalibrator:
    """
    Manages ArUco marker detection and automatic bin calibration.
    """

    def __init__(self):
        self._aruco_dict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT_TYPE)
        self._aruco_params = cv2.aruco.DetectorParameters()

        # ================================================================
        # DETECTOR HARDENING — preventing bit-flip misreads at the source
        # ================================================================
        # Context: DICT_4X4_50 has only a small Hamming distance between
        # some codes, so a single noisy pixel can decode a valid marker
        # as a DIFFERENT valid marker. Those misreads are what the
        # downstream sample-count and spacing filters catch after the
        # fact. The settings below try to stop the misread from
        # happening in the first place.

        # errorCorrectionRate=0.0 — reject ANY bit errors.
        # Default is 0.6 (accept a marker with up to 60% of its EC
        # capacity used). 0.3 was better but still allows 1 bit flip
        # through. Setting to 0.0 means the marker must decode
        # perfectly. This is the single biggest lever against bit-flip
        # misreads. Tradeoff: slightly lower detection rate at extreme
        # angles — acceptable because our sweep sees each marker in
        # many consecutive frames, so losing a borderline frame is
        # fine.
        try:
            self._aruco_params.errorCorrectionRate = 0.0
        except Exception:
            pass
        # Stricter marker border check. Default is 0.35 — up to 35% of
        # the black border bits can be wrong and the marker is still
        # accepted. Lowering to 0.04 means the border must be almost
        # perfectly black, which filters out partial/occluded/noisy
        # detections that are the usual source of bit-flip misreads.
        try:
            self._aruco_params.maxErroneousBitsInBorderRate = 0.04
        except Exception:
            pass
        # More samples per cell when reading the marker bits. Default
        # is 4 (4x4=16 samples per cell averaged). Raising to 8 (64
        # samples per cell) is far more robust against pixel noise and
        # motion blur — a single hot pixel can't flip a bit anymore.
        try:
            self._aruco_params.perspectiveRemovePixelPerCell = 8
        except Exception:
            pass
        # Stricter quadrilateral approximation. Default is 0.03 —
        # tightening to 0.01 rejects quads that aren't cleanly
        # rectangular, which is correlated with bad decodes.
        try:
            self._aruco_params.polygonalApproxAccuracyRate = 0.01
        except Exception:
            pass
        # Require higher image contrast for Otsu thresholding. Default
        # is 5.0 — raising to 8.0 rejects low-contrast regions where
        # bit errors are most likely.
        try:
            self._aruco_params.minOtsuStdDev = 8.0
        except Exception:
            pass
        # Subpixel corner refinement — markers detected at an angle or
        # near the frame edge get much more accurate centers, which
        # matters a lot for the zero-crossing interpolation.
        try:
            self._aruco_params.cornerRefinementMethod = (
                cv2.aruco.CORNER_REFINE_SUBPIX)
            self._aruco_params.cornerRefinementWinSize = 5
            self._aruco_params.cornerRefinementMaxIterations = 30
            self._aruco_params.cornerRefinementMinAccuracy = 0.1
        except Exception:
            pass
        # Reject markers that are very small or very large (both common
        # false-positive regimes). The perimeter rate is relative to the
        # image's min(width, height).
        try:
            self._aruco_params.minMarkerPerimeterRate = 0.04
            self._aruco_params.maxMarkerPerimeterRate = 4.0
        except Exception:
            pass
        # Wider adaptive threshold window range helps with uneven
        # lighting across the bin floor.
        try:
            self._aruco_params.adaptiveThreshWinSizeMin = 5
            self._aruco_params.adaptiveThreshWinSizeMax = 35
            self._aruco_params.adaptiveThreshWinSizeStep = 6
        except Exception:
            pass
        # NOTE: if bit-flip misreads STILL get through after all of the
        # above, the definitive fix is to switch ARUCO_DICT_TYPE at the
        # top of this file to DICT_5X5_100 or DICT_6X6_250. Those
        # dictionaries have much larger Hamming distances between
        # codes, so a single bit flip decodes to "no marker" instead
        # of a wrong valid ID. Requires reprinting the physical
        # markers.

        self._detector = cv2.aruco.ArucoDetector(self._aruco_dict, self._aruco_params)

        # Calibration state
        self.running = False
        self.progress = 0
        self.total_steps = 0
        self.message = ''
        self.discovered_bins = {}  # {marker_id: {'x': float, 'type': 'source'|'dest'}}
        self.provisional_bins = {}  # Low-sample markers pending re-verification
        self.calibration_result = None

        # Camera offset (can be updated via calibration)
        self.camera_x_offset = CAMERA_X_OFFSET
        self.max_sweep_x = DEFAULT_MAX_SWEEP_X

        # Empty bin detection results
        self.empty_bins = {}  # {bin_number: bool}  True = empty (marker visible)

        self._emit = None
        self._lock = threading.Lock()
        # Serializes access to the ArUco detector. OpenCV's ArucoDetector
        # holds internal state that is NOT thread-safe; both the sweep
        # worker thread and the MJPEG preview thread call detect_markers(),
        # so every call must go through this lock to prevent corrupted
        # state or hangs inside the native detector.
        self._detect_lock = threading.Lock()

    def set_emit(self, emit_fn):
        """Set the SocketIO emit callback."""
        self._emit = emit_fn

    def emit(self, event, data):
        if self._emit:
            self._emit(event, data)

    def detect_markers(self, frame):
        """
        Detect ArUco markers in a frame.

        Returns list of dicts:
            [{'id': int, 'corners': np.array, 'center': (cx, cy)}]

        Thread-safe: serializes access to the underlying ArucoDetector via
        `self._detect_lock`. The gray conversion is done outside the lock
        so only the actual detector call is serialized, keeping contention
        minimal between the sweep worker and the MJPEG preview thread.
        """
        if frame is None:
            return []

        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        except Exception:
            return []

        try:
            with self._detect_lock:
                corners, ids, rejected = self._detector.detectMarkers(gray)
        except Exception:
            # Detector blew up — return empty rather than propagating so
            # neither the sweep nor the preview stream crashes.
            return []

        results = []
        if ids is not None:
            for i, marker_id in enumerate(ids.flatten()):
                corner_pts = corners[i][0]
                cx = int(np.mean(corner_pts[:, 0]))
                cy = int(np.mean(corner_pts[:, 1]))
                results.append({
                    'id': int(marker_id),
                    'corners': corner_pts,
                    'center': (cx, cy),
                })
        return results

    def get_marker_type(self, marker_id):
        """Return 'source', 'dest', or 'staging' based on marker ID range."""
        if marker_id in SOURCE_MARKER_IDS:
            return 'source'
        elif marker_id == STAGING_MARKER_ID:
            return 'staging'
        elif marker_id in DEST_MARKER_IDS:
            return 'dest'
        return 'unknown'

    def draw_markers_on_frame(self, frame, markers):
        """Draw detected markers on a frame for visualization."""
        if frame is None:
            return frame
        out = frame.copy()
        for m in markers:
            pts = m['corners'].astype(int)
            cv2.polylines(out, [pts], True, (0, 255, 0), 2)
            cx, cy = m['center']
            label = f"ID:{m['id']} ({self.get_marker_type(m['id'])})"
            cv2.putText(out, label, (cx - 40, cy - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
            # Draw center crosshair
            cv2.drawMarker(out, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 10, 2)
        # Draw frame center reference
        h, w = out.shape[:2]
        cv2.drawMarker(out, (w // 2, h // 2), (255, 255, 0),
                        cv2.MARKER_CROSS, 15, 1)
        return out

    def run_sweep_calibration(self, camera, gcode_module, expected_sources=1,
                               expected_dests=10, expect_staging=True,
                               max_sweep_x=None, sweep_feedrate=None):
        """
        Run the full calibration sweep.

        Strategy: at every sweep step, detect every marker in frame and
        for each one record the signed pixel offset from frame center
        along with the carriage X. A marker is visible across many
        consecutive frames as the camera crosses over it, so by the end
        of the sweep we have a series of (carriage_x, px_offset) samples
        per marker. The 'true' centered carriage position for that
        marker is wherever px_offset crosses zero, which we find by
        linear interpolation between the two samples bracketing zero
        (or the closest sample if no bracket exists).

        Advantages over the old fine-center-as-you-go approach:
        - Multiple markers in one frame are all handled correctly.
        - No destructive motion: the carriage only moves forward, in a
          predictable sweep, so the MJPEG preview stays in sync.
        - A marker found near the left edge of the frame is recorded
          AND the marker at the right edge of the same frame is too.
        - Sub-step resolution from interpolation, no extra motion needed.

        Must be called from the worker thread (uses serial I/O).
        """
        with self._lock:
            if self.running:
                return {'error': 'Calibration already running'}
            self.running = True

        sweep_max = max_sweep_x or self.max_sweep_x
        # Persist the effective sweep_max back onto the instance so every
        # downstream helper (the false-positive filter, the refinement
        # pass, and the fine-center routine) sees the SAME bounds the
        # sweep actually used. Without this, setting a custom Max Sweep X
        # in the UI would correctly drive the carriage farther, but the
        # filter would still reject markers outside the old default
        # range and the refinement clamp would pull positions back in —
        # making the UI value look like it was being ignored.
        self.max_sweep_x = float(sweep_max)
        # Relax the gcode safety clamp to cover the full sweep range plus
        # a margin for the camera offset. Without this, any G0 X command
        # past the default X_SAFE_MAX silently gets rewritten down to the
        # ceiling and the sweep dead-ends. set_x_safe_max only grows the
        # limit, never shrinks — it's safe to call with any value.
        try:
            margin = max(50.0, abs(float(self.camera_x_offset)) + 50.0)
            gcode_module.set_x_safe_max(self.max_sweep_x + margin)
        except Exception as e:
            print(f"[calibration] could not update X_SAFE_MAX: {e}")
        # Belt-and-suspenders: re-assert M211 S0 (software endstops off)
        # at sweep start. The firmware's X_MAX_POS is 800mm but our rail
        # travels farther, and Marlin SILENTLY clips G0 X>800 to X=800
        # while still returning "ok" — producing the "doesn't probe
        # bins past a certain X" failure mode. connect_to_board() sends
        # this once at startup, but re-sending it here covers the case
        # of a reconnect-without-restart or an M999 that restored
        # firmware defaults mid-session.
        try:
            gcode_module._send_and_wait("M211 S0")
        except Exception as e:
            print(f"[calibration] could not disable software endstops: {e}")
        feedrate = sweep_feedrate or SWEEP_FEEDRATE
        expected_total = expected_sources + expected_dests + (1 if expect_staging else 0)

        self.discovered_bins = {}
        self.provisional_bins = {}
        self.progress = 0
        # Sweep range: the carriage walks from X=0 up to X=sweep_max.
        # During that, given the camera offset convention
        #   camera_world_x = carriage_x + CAMERA_X_OFFSET
        # the camera's eye passes through world X values in the inclusive
        # range [CAMERA_X_OFFSET, sweep_max + CAMERA_X_OFFSET].
        #
        # Blind zones (the user's bins must NOT fall into these):
        #   - If camera_x_offset > 0 (camera right of head), world X values
        #     below camera_x_offset can never be seen (carriage can't go
        #     negative).
        #   - If camera_x_offset < 0 (camera left of head), world X values
        #     above (sweep_max + camera_x_offset) can never be seen.
        carriage_max = float(sweep_max)
        self.total_steps = max(int(carriage_max / SWEEP_STEP_SIZE), 1)
        off = self.camera_x_offset
        camera_range_lo = round(0 + off, 1)
        camera_range_hi = round(carriage_max + off, 1)
        self.message = (f'Starting sweep — carriage 0..{carriage_max:.0f}mm '
                        f'(camera sees world X {camera_range_lo}..{camera_range_hi}mm)')
        self.emit('calibration_progress', self._get_status())

        # Per-marker observation history:
        # marker_obs[mid] = [(carriage_x, signed_px_offset_from_center), ...]
        marker_obs = {}
        marker_type_map = {}
        # Once a marker leaves the frame after being seen, it's "locked in"
        # and ignored for the rest of the sweep.
        locked_markers = set()
        # Markers that were visible in the *previous* frame. Used to detect
        # visible-to-not-visible transitions.
        prev_visible = set()
        # Last pixel-space center of each currently-tracking marker, used
        # for in-sweep spatial aliasing (bit-flip attribution). Key is
        # marker id, value is (pixel_x, pixel_y, sweep_step_index).
        marker_last_pixel = {}
        # A new detection within PIXEL_ALIAS_THRESHOLD px of a different
        # marker's recently-observed pixel center is almost certainly
        # a bit-flip misread of that other marker — real bins are >60mm
        # apart which is hundreds of pixels in camera space.
        PIXEL_ALIAS_THRESHOLD = 40.0
        ALIAS_FRAME_WINDOW = 3  # only consider recent history

        # Local cancel flag — set to True if the user cancelled mid-sweep
        # so the caller can distinguish "cancelled partway" from "finished
        # naturally". Returned in the result dict as `cancelled`.
        was_cancelled = False

        try:
            # Ensure Z is up
            self.message = 'Raising Z to safe height...'
            self.emit('calibration_progress', self._get_status())
            gcode_module._send_and_wait(f"G0 Z{gcode_module.Z_MAX} F{gcode_module.Z_FEEDRATE}")
            gcode_module._send_and_wait("M400")

            if not self.running:
                was_cancelled = True

            # Home X
            if not was_cancelled:
                self.message = 'Homing X axis...'
                self.emit('calibration_progress', self._get_status())
                gcode_module._send_and_wait("G28 X")
                gcode_module._send_and_wait("M400")
                if not self.running:
                    was_cancelled = True

            current_x = 0.0
            last_visible = set()

            # Single forward sweep, no backtracking
            while not was_cancelled and current_x <= carriage_max:
                if not self.running:
                    self.message = 'Calibration cancelled'
                    was_cancelled = True
                    break

                self.progress = int(current_x / SWEEP_STEP_SIZE)
                self.message = (f'Scanning X={current_x:.0f}mm '
                                f'({len(locked_markers)} locked, '
                                f'{len(marker_obs) - len(locked_markers)} tracking)')
                self.emit('calibration_progress', self._get_status())

                # Move to position
                gcode_module._send_and_wait(f"G0 X{current_x} F{feedrate}")
                gcode_module._send_and_wait("M400")
                time.sleep(0.15)  # Let vibrations settle

                # Wait for a genuinely fresh frame (no nulling — keeps the
                # live preview running smoothly).
                try:
                    camera.flush_buffer(min_new_frames=2, timeout=0.5)
                except Exception:
                    pass

                frame = camera.get_frame()
                if frame is None:
                    current_x += SWEEP_STEP_SIZE
                    continue

                frame_center_x = frame.shape[1] // 2
                markers = self.detect_markers(frame)

                # Record observations for every non-locked in-range marker
                visible_ids = set()
                step_idx = self.progress
                for m in markers:
                    mid = int(m['id'])
                    mtype = self.get_marker_type(mid)
                    if mtype == 'unknown':
                        continue
                    if mid in locked_markers:
                        # Already finalized — ignore any re-detection
                        continue
                    cx = m['center'][0]
                    cy = m['center'][1]

                    # --- In-sweep bit-flip attribution ---
                    # If this detection sits at nearly the same pixel as
                    # a DIFFERENT marker we're already tracking, treat
                    # it as a bit-flip misread of that other marker and
                    # merge the observation into the existing track.
                    # This stops ghost IDs from ever accumulating their
                    # own samples during the sweep.
                    aliased_to = None
                    best_samples = -1
                    for other_mid, (opx, opy, ostep) in marker_last_pixel.items():
                        if other_mid == mid:
                            continue
                        if other_mid in locked_markers:
                            continue
                        if step_idx - ostep > ALIAS_FRAME_WINDOW:
                            continue
                        dx = cx - opx
                        dy = cy - opy
                        if (dx * dx + dy * dy) > (PIXEL_ALIAS_THRESHOLD ** 2):
                            continue
                        other_samples = len(marker_obs.get(other_mid, []))
                        my_samples = len(marker_obs.get(mid, []))
                        # Only alias if the "other" marker has strictly
                        # more evidence than this one. This way, the
                        # majority reading wins and short-lived bit
                        # flips are absorbed into the real marker.
                        if other_samples > my_samples and other_samples > best_samples:
                            aliased_to = other_mid
                            best_samples = other_samples

                    target_mid = aliased_to if aliased_to is not None else mid
                    target_mtype = marker_type_map.get(target_mid, mtype)

                    signed_offset = cx - frame_center_x  # +ve = right of center
                    marker_obs.setdefault(target_mid, []).append(
                        (current_x, signed_offset))
                    marker_type_map[target_mid] = target_mtype
                    marker_last_pixel[target_mid] = (cx, cy, step_idx)
                    visible_ids.add(target_mid)

                # Markers visible last step but gone this step are
                # fully observed — lock them in now.
                departed = prev_visible - visible_ids - locked_markers
                for mid in departed:
                    obs = marker_obs.get(mid, [])
                    if not obs:
                        continue
                    mtype = marker_type_map.get(mid, 'unknown')
                    centered_x = self._resolve_centered_x(obs)
                    # Convert: carriage X where camera is centered on marker
                    # --> world X where the marker physically lives
                    # --> head target for probing (head_world == marker_world).
                    # See CAMERA_X_OFFSET convention at the top of this file.
                    bin_x = centered_x + self.camera_x_offset
                    self.discovered_bins[mid] = {
                        'marker_id': mid,
                        'type': mtype,
                        'carriage_x_centered': centered_x,
                        'bin_x': bin_x,
                        'sample_count': len(obs),
                    }
                    locked_markers.add(mid)
                    self.log_lock(mid, mtype, bin_x, len(obs))
                prev_visible = visible_ids

                # Emit a live "currently visible" update whenever the set
                # changes, so the UI can show what the camera sees in
                # real time during the sweep.
                if visible_ids != last_visible:
                    self.emit('calibration_marker_visible', {
                        'carriage_x': round(current_x, 1),
                        'markers': [
                            {'id': mid, 'type': marker_type_map[mid]}
                            for mid in sorted(visible_ids)
                        ],
                        'locked_count': len(locked_markers),
                    })
                    last_visible = visible_ids

                # Early exit: all expected markers are locked in — but we
                # must count only markers that would SURVIVE the
                # false-positive filter. A bit-flip misread typically has
                # 1-2 samples and sits right next to a real neighbor, so
                # it's guaranteed to be dropped later. Counting it toward
                # the expected total would make the sweep bail before it
                # reaches the real markers further out, which is exactly
                # the failure mode we were seeing.
                effective_locked, _, _ = self._filter_false_positives(
                    self.discovered_bins)
                if len(effective_locked) >= expected_total:
                    self.message = (f'All {expected_total} expected markers '
                                    f'locked — stopping sweep early')
                    self.emit('calibration_progress', self._get_status())
                    break

                current_x += SWEEP_STEP_SIZE

            # --- Sweep done. Any markers still in-frame at sweep end (or
            # ones we only saw in one frame and then lost before the
            # "departed" detection kicked in) won't have been locked yet.
            # Resolve them now.
            unresolved = [
                mid for mid in marker_obs
                if mid not in locked_markers
            ]
            if unresolved:
                self.message = (f'Sweep complete — resolving '
                                f'{len(unresolved)} remaining markers')
                self.emit('calibration_progress', self._get_status())
                for mid in unresolved:
                    obs = marker_obs[mid]
                    if not obs:
                        continue
                    mtype = marker_type_map[mid]
                    centered_x = self._resolve_centered_x(obs)
                    # Same world-X conversion as in the live lock path above.
                    bin_x = centered_x + self.camera_x_offset
                    self.discovered_bins[mid] = {
                        'marker_id': mid,
                        'type': mtype,
                        'carriage_x_centered': centered_x,
                        'bin_x': bin_x,
                        'sample_count': len(obs),
                    }
                    locked_markers.add(mid)
                    self.log_lock(mid, mtype, bin_x, len(obs))

            # If the user cancelled, return a distinct "cancelled" result
            # so the caller can skip the "apply positions + probe bins"
            # follow-up. We do NOT build or return a bin config in this
            # case — partial results from an interrupted sweep are not
            # trustworthy enough to program the machine with.
            if was_cancelled:
                self.message = 'Calibration cancelled by user'
                self.emit('calibration_progress', self._get_status())
                self.emit('calibration_complete',
                          {'cancelled': True, 'error': 'cancelled'})
                return {'cancelled': True, 'error': 'cancelled'}

            # --- False-positive filter: drop markers seen in too few
            # frames and collapse clusters that are physically too close
            # to each other. See _filter_false_positives() for details.
            #
            # Returns three buckets: `filtered` = definitely kept,
            # `provisional` = "too few samples" markers that will be
            # re-verified during the refinement pass, and `rejections`
            # = confirmed false positives (bounds errors or cluster
            # collisions) that are permanently dropped.
            raw_count = len(self.discovered_bins)
            filtered, provisional, rejections = self._filter_false_positives(
                self.discovered_bins)
            self.discovered_bins = filtered
            self.provisional_bins = provisional
            if rejections:
                summary = (
                    f"Filtered {len(rejections)} suspect marker(s) "
                    f"from {raw_count} raw detections")
                print(f"[calibration] {summary}")
                self.emit('log_message', {'message': summary})
                for rej in rejections:
                    line = (
                        f"  rejected ID {rej['marker_id']} "
                        f"({rej['type']}) at X={rej['bin_x']:.1f}mm: "
                        f"{rej['reason']}")
                    print(f"[calibration] {line}")
                    self.emit('log_message', {'message': line})
                self.emit('calibration_rejections',
                          {'rejections': rejections})
            if provisional:
                prov_line = (
                    f"{len(provisional)} provisional marker(s) with "
                    f"<{MIN_MARKER_SAMPLES} samples queued for "
                    f"re-verification during refinement: "
                    f"{sorted(provisional.keys())}")
                print(f"[calibration] {prov_line}")
                self.emit('log_message', {'message': prov_line})

            # --- Provisional verification pass: drive to each marker
            # that had too few samples during the sweep and re-check
            # its visibility at a stable position. Markers that really
            # exist get promoted into discovered_bins and will be
            # refined in the next pass; bit-flips / ghosts that never
            # reappear stay dropped. Non-fatal if it hits an error.
            self._last_verify_rejections = []
            try:
                self._verify_provisional_bins(camera, gcode_module)
            except Exception as e:
                print(f"[calibration] provisional verification failed: {e}")
                self.emit('log_message', {
                    'message': (f'Provisional verification pass failed: '
                                f'{e} (dropping unverified markers)'),
                })

            # Any markers that failed re-verification need to end up
            # in the public `rejections` list so the UI / logs show
            # them alongside the sweep-time rejections.
            if self._last_verify_rejections:
                for rej in self._last_verify_rejections:
                    line = (
                        f"  rejected ID {rej['marker_id']} "
                        f"({rej['type']}) at X={rej['bin_x']:.1f}mm: "
                        f"{rej['reason']}")
                    print(f"[calibration] {line}")
                    self.emit('log_message', {'message': line})
                rejections.extend(self._last_verify_rejections)
                self.emit('calibration_rejections',
                          {'rejections': self._last_verify_rejections})

            # --- Refinement pass: drive to each surviving marker and
            # nudge until it's pixel-centered in the frame. This takes
            # the interpolated positions (accurate to ~1mm) and tightens
            # them to sub-millimeter using visual servoing. Non-fatal
            # if it hits an error — we fall back to the interpolated
            # values. Skipped entirely if the sweep was cancelled.
            try:
                self._refine_bin_positions(camera, gcode_module)
            except Exception as e:
                print(f"[calibration] refinement pass failed: {e}")
                self.emit('log_message', {
                    'message': (f'Bin refinement pass failed: {e} '
                                f'(keeping interpolated positions)'),
                })

            # Build final bin configuration
            self.calibration_result = self._build_bin_config()
            self.calibration_result['rejections'] = rejections
            found_ids_sorted = sorted(self.discovered_bins.keys())
            self.message = (
                f'Calibration complete: {len(self.discovered_bins)} markers kept '
                f'({len(rejections)} rejected) '
                f'— IDs: {", ".join(str(i) for i in found_ids_sorted) or "none"}'
            )
            self.progress = self.total_steps
            self.emit('calibration_progress', self._get_status())
            self.emit('calibration_complete', self.calibration_result)

            return self.calibration_result

        except Exception as e:
            self.message = f'Calibration error: {e}'
            self.emit('calibration_progress', self._get_status())
            return {'error': str(e)}

        finally:
            self.running = False

    def log_lock(self, marker_id, mtype, bin_x, sample_count):
        """Emit a 'marker locked' progress + event when a marker is finalized."""
        self.message = (f'Locked marker ID {marker_id} ({mtype}): '
                        f'bin X = {bin_x:.1f}mm ({sample_count} samples)')
        self.emit('calibration_progress', self._get_status())
        self.emit('calibration_marker_found', {
            'marker_id': int(marker_id),
            'type': mtype,
            'bin_x': round(bin_x, 1),
            'sample_count': int(sample_count),
        })

    def _filter_false_positives(self, discovered_bins):
        """
        Post-sweep cleanup of `discovered_bins` dict. Three buckets:

          1. Kept — physically in range, enough samples, no cluster
             collision. These survive and become real bins.

          2. Provisional — physically in range, cluster-free, but had
             FEWER than MIN_MARKER_SAMPLES observations. These are
             suspicious but not condemned; the refinement pass will
             drive back to each one and try to re-verify it with a
             long stable-position burst of frames. If the marker
             really exists there, it gets promoted.

          3. Rejections — confirmed false positives. Markers whose
             resolved X is outside the sweep range (extrapolated
             noise) or collide physically with another higher-sample
             marker (bit-flip ghosts).

        Returns a tuple of (kept_bins, provisional_bins, rejections).
        `rejections` is a list of dicts describing every dropped marker
        and why, so we can log / emit them to the UI for diagnostics.
        Callers that only care about the final survivor count can
        ignore `provisional_bins`.
        """
        if not discovered_bins:
            return {}, {}, []

        rejections = []
        kept = {}
        provisional = {}

        # --- Pass 0: drop markers whose resolved world X is physically
        # unreachable. The head can only reach carriage X in
        # [0, max_sweep_x], so a bin_x outside [0, max_sweep_x] is
        # extrapolated noise from linear interpolation past the actual
        # sampled range. Also reject any carriage_x_centered that lands
        # outside the swept window — that means we never actually saw
        # the marker's zero-crossing and the "position" is a guess.
        sweep_lo = 0.0
        sweep_hi = float(self.max_sweep_x)
        # World X is (carriage_x + camera_x_offset), so the visible
        # world-X window is:
        world_lo = sweep_lo + self.camera_x_offset
        world_hi = sweep_hi + self.camera_x_offset
        in_bounds = []
        for mid, info in discovered_bins.items():
            cx = float(info.get('carriage_x_centered', 0.0))
            bx = float(info.get('bin_x', 0.0))
            # Allow a small tolerance (1mm) for boundary markers that
            # landed exactly on the edge of the sweep range.
            TOL = 1.0
            if cx < (sweep_lo - TOL) or cx > (sweep_hi + TOL):
                rejections.append({
                    'marker_id': int(mid),
                    'type': info.get('type', 'unknown'),
                    'bin_x': round(bx, 1),
                    'sample_count': int(info.get('sample_count', 0)),
                    'reason': (f'carriage X {cx:.1f}mm out of sweep '
                               f'range [{sweep_lo:.0f}, {sweep_hi:.0f}] '
                               f'— likely extrapolated'),
                })
                continue
            if bx < (world_lo - TOL) or bx > (world_hi + TOL):
                rejections.append({
                    'marker_id': int(mid),
                    'type': info.get('type', 'unknown'),
                    'bin_x': round(bx, 1),
                    'sample_count': int(info.get('sample_count', 0)),
                    'reason': (f'world X {bx:.1f}mm out of visible range '
                               f'[{world_lo:.0f}, {world_hi:.0f}]'),
                })
                continue
            in_bounds.append((mid, info))

        # --- Pass 0b: staging platform exclusion zone ---
        # If the staging marker (ID 49) is among the in-bounds markers,
        # reject any OTHER marker whose world X falls within half the
        # staging platform width. These are almost always bit-flip
        # ghosts or reflections from the staging marker itself — the
        # platform is a single large surface with no room for a bin.
        staging_x = None
        for mid, info in in_bounds:
            if int(mid) == STAGING_MARKER_ID:
                staging_x = float(info.get('bin_x', 0.0))
                break
        if staging_x is not None:
            half_w = STAGING_PLATFORM_WIDTH_MM / 2.0
            filtered_bounds = []
            for mid, info in in_bounds:
                if int(mid) == STAGING_MARKER_ID:
                    filtered_bounds.append((mid, info))
                    continue
                bx = float(info.get('bin_x', 0.0))
                if abs(bx - staging_x) < half_w:
                    rejections.append({
                        'marker_id': int(mid),
                        'type': info.get('type', 'unknown'),
                        'bin_x': round(bx, 1),
                        'sample_count': int(info.get('sample_count', 0)),
                        'reason': (f'inside staging platform exclusion zone '
                                   f'(ΔX={abs(bx - staging_x):.1f}mm < '
                                   f'{half_w:.0f}mm from staging at '
                                   f'{staging_x:.1f}mm)'),
                    })
                    continue
                filtered_bounds.append((mid, info))
            in_bounds = filtered_bounds

        # --- Pass 1: separate markers with too few samples into the
        # provisional bucket (not rejected outright). The refinement
        # pass will re-verify each provisional marker by driving to
        # its interpolated position and reading many frames; real
        # markers that happened to miss frames during the sweep will
        # show up there and get promoted.
        candidates = []
        for mid, info in in_bounds:
            samples = info.get('sample_count', 0)
            if samples < MIN_MARKER_SAMPLES:
                provisional[int(mid)] = info
                continue
            candidates.append(info)

        # --- Pass 2: collapse clusters within MIN_BIN_SPACING_MM ---
        # Sort by confidence (most samples first) so greedy acceptance
        # always keeps the best candidate.
        candidates.sort(key=lambda b: (-int(b.get('sample_count', 0)),
                                        float(b.get('bin_x', 0.0))))
        for info in candidates:
            bx = float(info.get('bin_x', 0.0))
            collision = None
            for other in kept.values():
                if abs(bx - float(other.get('bin_x', 0.0))) < MIN_BIN_SPACING_MM:
                    collision = other
                    break
            if collision is not None:
                rejections.append({
                    'marker_id': int(info.get('marker_id', -1)),
                    'type': info.get('type', 'unknown'),
                    'bin_x': round(bx, 1),
                    'sample_count': int(info.get('sample_count', 0)),
                    'reason': (f'too close to marker '
                               f'{collision.get("marker_id")} '
                               f'(ΔX={abs(bx - collision.get("bin_x", 0.0)):.1f}mm '
                               f'< {MIN_BIN_SPACING_MM}mm)'),
                })
                continue
            kept[int(info.get('marker_id', -1))] = info

        return kept, provisional, rejections

    def _resolve_centered_x(self, observations):
        """
        Given a list of (carriage_x, signed_px_offset) observations for a
        single marker, return the carriage X at which the pixel offset
        would be zero (i.e. the marker is centered in the frame).

        Algorithm:
          1. Walk the observations looking for a sign change (zero crossing).
             If found, linearly interpolate between the two bracketing
             samples to estimate the exact crossing point.
          2. If no sign change exists (marker entered or left the frame
             before reaching the center), fall back to the sample with
             the smallest absolute offset.
        """
        if not observations:
            return 0.0
        # Sort by carriage_x just in case
        obs = sorted(observations, key=lambda o: o[0])
        # Look for a sign change between consecutive samples
        for i in range(len(obs) - 1):
            x1, p1 = obs[i]
            x2, p2 = obs[i + 1]
            if (p1 == 0) or (p1 > 0 and p2 <= 0) or (p1 < 0 and p2 >= 0):
                if p1 == p2:
                    return (x1 + x2) / 2.0
                # Linear interpolation for zero crossing
                t = p1 / (p1 - p2)
                return x1 + t * (x2 - x1)
        # No bracket — use sample closest to zero
        best = min(obs, key=lambda o: abs(o[1]))
        return best[0]

    def _read_marker_offset(self, camera, marker_id, samples=5):
        """
        Read `samples` fresh frames and return the MEDIAN signed pixel
        offset of `marker_id` from frame center. Returns None if the
        marker isn't visible in any frame.

        Using the median (rather than a single frame) absorbs detector
        noise, shutter latency, and tiny vibration between frames —
        without which the refinement loop can bias by several pixels
        toward whichever direction the last transient was.
        """
        offsets = []
        for _ in range(samples):
            try:
                camera.flush_buffer()
            except Exception:
                pass
            frame = camera.get_frame()
            if frame is None:
                time.sleep(0.03)
                continue
            markers = self.detect_markers(frame)
            target = None
            for m in markers:
                if m['id'] == marker_id:
                    target = m
                    break
            if target is None:
                time.sleep(0.03)
                continue
            cx, _cy = target['center']
            frame_center_x = frame.shape[1] // 2
            offsets.append(float(cx - frame_center_x))
            time.sleep(0.02)
        if not offsets:
            return None
        offsets.sort()
        return offsets[len(offsets) // 2]

    def _verify_provisional_bins(self, camera, gcode_module):
        """
        Second-chance verification for markers that fell below the
        MIN_MARKER_SAMPLES threshold during the sweep.

        A marker can legitimately collect fewer than 3 sweep samples
        when the camera's 10mm step size lands its zero-crossing
        between frames, or when a couple of frames get dropped at
        USB/shutter boundaries. Rather than throwing those markers
        away outright, we physically drive back to each one's
        interpolated X and take a long burst of frames at that
        stable position. A real marker will now show up in most of
        those frames (it was only "rare" before because we were
        moving past it); a bit-flip or reflection ghost won't
        reappear and stays rejected.

        Promoted markers are inserted into `self.discovered_bins`
        with a `promoted_from_provisional` flag and a refreshed
        sample_count, so that the subsequent refinement pass
        operates on them normally (slope probe + correction).

        Cluster-collision spacing is re-checked at promotion time
        against the already-kept markers — a bit-flip ghost of a
        real marker will land within MIN_BIN_SPACING_MM of the real
        one and be rejected here even if its ID passes the visibility
        check.
        """
        if not self.provisional_bins:
            return

        feedrate = 6000

        total = len(self.provisional_bins)
        self.message = f'Re-verifying {total} provisional marker(s)...'
        self.emit('calibration_progress', self._get_status())
        self.emit('log_message', {
            'message': (f'Starting provisional marker re-verification '
                        f'pass ({total} markers)'),
        })

        # Sort by interpolated carriage X so we travel in physical
        # order — minimizes total travel and keeps the carriage going
        # in the same direction as the previous refinement pass would
        # start with.
        sorted_prov = sorted(
            self.provisional_bins.items(),
            key=lambda item: item[1].get('carriage_x_centered', 0.0))

        promoted = 0
        dropped = 0
        drop_records = []

        def _drive_and_settle(x):
            x_clamped = max(0.0, min(float(x), float(self.max_sweep_x)))
            try:
                gcode_module._send_and_wait(
                    f"G0 X{x_clamped:.2f} F{feedrate}")
                gcode_module._send_and_wait("M400")
            except Exception as e:
                print(f"[calibration] verify move failed: {e}")
                return None
            time.sleep(0.25)
            return x_clamped

        for mid, info in sorted_prov:
            if not self.running:
                self.emit('log_message', {
                    'message': 'Provisional verification cancelled',
                })
                return

            start_x = float(info.get('carriage_x_centered', 0.0))
            mtype = info.get('type', 'unknown')
            orig_samples = int(info.get('sample_count', 0))

            pos = _drive_and_settle(start_x)
            if pos is None:
                dropped += 1
                drop_records.append({
                    'marker_id': int(mid),
                    'type': mtype,
                    'bin_x': round(info.get('bin_x', 0.0), 1),
                    'sample_count': orig_samples,
                    'reason': 'verify move failed',
                })
                continue

            # Extra settle: the USB camera's autoexposure / AWB
            # needs a beat or two to recover from the motion blur
            # it saw during the jump. Without this, the burst below
            # can't decode the marker even when it's sitting right
            # in the middle of the frame.
            time.sleep(PROVISIONAL_VERIFY_SETTLE_SEC)
            # Discard warmup frames so the first "real" read sees a
            # properly-exposed image. Flush the buffer each time so
            # we don't just chew through a stale queue.
            for _ in range(PROVISIONAL_VERIFY_WARMUP_FRAMES):
                try:
                    camera.flush_buffer()
                except Exception:
                    pass
                _ = camera.get_frame()
                time.sleep(0.05)

            # Burst-read PROVISIONAL_VERIFY_FRAMES frames at the
            # stable position and count how many of them actually see
            # the target marker. Also compute the median pixel offset
            # from the hits so we can update carriage_x_centered for
            # the refinement pass to work from a more accurate start.
            hits = 0
            offsets = []
            for _ in range(PROVISIONAL_VERIFY_FRAMES):
                try:
                    camera.flush_buffer()
                except Exception:
                    pass
                frame = camera.get_frame()
                if frame is None:
                    time.sleep(0.02)
                    continue
                markers = self.detect_markers(frame)
                found = None
                for m in markers:
                    if int(m['id']) == int(mid):
                        found = m
                        break
                if found is None:
                    time.sleep(0.02)
                    continue
                hits += 1
                cx, _cy = found['center']
                frame_center_x = frame.shape[1] // 2
                offsets.append(float(cx - frame_center_x))
                time.sleep(0.02)

            if hits < PROVISIONAL_VERIFY_MIN_HITS:
                dropped += 1
                drop_records.append({
                    'marker_id': int(mid),
                    'type': mtype,
                    'bin_x': round(info.get('bin_x', 0.0), 1),
                    'sample_count': orig_samples,
                    'reason': (f'failed re-verify '
                               f'({hits}/{PROVISIONAL_VERIFY_FRAMES} '
                               f'hits, need {PROVISIONAL_VERIFY_MIN_HITS})'),
                })
                self.emit('log_message', {
                    'message': (f'Verify: marker {mid} ({mtype}) NOT '
                                f'reappearing at X={start_x:.2f} '
                                f'({hits}/{PROVISIONAL_VERIFY_FRAMES} '
                                f'hits) — dropping'),
                })
                continue

            # Build an updated carriage_x_centered estimate from the
            # observed median pixel offset. Without knowing px/mm yet
            # we can't convert it to a position correction, so we just
            # keep the interpolated start_x — the refinement pass
            # will run its own slope probe from here and dial it in.
            offsets.sort()
            median_off = offsets[len(offsets) // 2]

            # Staging exclusion zone: if this provisional marker falls
            # within the staging platform footprint, it's almost certainly
            # a bit-flip ghost from the staging marker.
            bx = float(info.get('bin_x', 0.0))
            staging_info = self.discovered_bins.get(STAGING_MARKER_ID)
            if staging_info is not None and int(mid) != STAGING_MARKER_ID:
                staging_bx = float(staging_info.get('bin_x', 0.0))
                half_w = STAGING_PLATFORM_WIDTH_MM / 2.0
                if abs(bx - staging_bx) < half_w:
                    dropped += 1
                    drop_records.append({
                        'marker_id': int(mid),
                        'type': mtype,
                        'bin_x': round(bx, 1),
                        'sample_count': orig_samples,
                        'reason': (f'inside staging platform exclusion zone '
                                   f'(ΔX={abs(bx - staging_bx):.1f}mm < '
                                   f'{half_w:.0f}mm from staging)'),
                    })
                    self.emit('log_message', {
                        'message': (f'Verify: marker {mid} visible but '
                                    f'inside staging exclusion zone — '
                                    f'dropping'),
                    })
                    continue

            # Cluster-collision re-check: if this promoted marker is
            # within MIN_BIN_SPACING_MM of a marker we already kept,
            # it's a bit-flip of that marker and must be dropped.
            collision = None
            for other_mid, other in self.discovered_bins.items():
                if abs(bx - float(other.get('bin_x', 0.0))) < MIN_BIN_SPACING_MM:
                    collision = other_mid
                    break
            if collision is not None:
                dropped += 1
                drop_records.append({
                    'marker_id': int(mid),
                    'type': mtype,
                    'bin_x': round(bx, 1),
                    'sample_count': orig_samples,
                    'reason': (f'verified but within '
                               f'{MIN_BIN_SPACING_MM:.0f}mm of marker '
                               f'{collision} — bit-flip ghost'),
                })
                self.emit('log_message', {
                    'message': (f'Verify: marker {mid} visible '
                                f'({hits}/{PROVISIONAL_VERIFY_FRAMES}) '
                                f'but collides with marker {collision} '
                                f'— dropping as bit-flip ghost'),
                })
                continue

            # Promote into discovered_bins with the refreshed sample
            # count. The refinement pass takes over from here.
            info['sample_count'] = orig_samples + hits
            info['promoted_from_provisional'] = True
            info['verify_hits'] = hits
            info['verify_median_offset_px'] = round(median_off, 1)
            self.discovered_bins[int(mid)] = info
            promoted += 1
            self.log_lock(int(mid), mtype, bx, info['sample_count'])
            self.emit('log_message', {
                'message': (f'Verify: marker {mid} ({mtype}) PROMOTED — '
                            f'seen in {hits}/{PROVISIONAL_VERIFY_FRAMES} '
                            f'stable frames at X={start_x:.2f} '
                            f'(median px offset {median_off:+.1f})'),
            })

        # Clear the provisional dict — anything left there was not
        # promoted. We add drop records to `calibration_result` later
        # via the rejection mechanism (they'll be emitted by the main
        # sweep flow after this method returns).
        self._last_verify_rejections = drop_records
        self.provisional_bins = {}

        summary = (f'Provisional verification done: '
                   f'{promoted} promoted, {dropped} dropped')
        print(f'[calibration] {summary}')
        self.emit('log_message', {'message': summary})
        self.emit('calibration_progress', self._get_status())

    def _refine_bin_positions(self, camera, gcode_module):
        """
        Second-pass visual refinement of discovered bin X positions.

        The sweep interpolation is accurate to ~1mm, but that's limited
        by the 10mm sweep step size and single-frame marker detection
        noise. This pass physically drives to each bin's estimated
        center, then uses a SELF-CALIBRATING two-point measurement to
        compute the exact carriage X where the marker is at frame
        center.

        Algorithm (per marker):
          1. Drive to interpolated carriage_x_centered. Let settle.
          2. Read median pixel offset → offset_0.
          3. Drive +PROBE_STEP mm. Let settle.
          4. Read median pixel offset → offset_1.
          5. Compute slope: px_per_mm = (offset_1 - offset_0) / PROBE_STEP
             — whatever its sign happens to be. The sign depends on
             camera rotation and mounting; we don't assume it.
          6. Solve for zero crossing:
                target_x = start_x + (-offset_0 / px_per_mm)
          7. Drive to target_x. Verify with one final read. If residual
             |offset| > FINE_CENTER_TOLERANCE, do one more correction.

        This converges in 3 moves instead of 40 iterations, is robust
        to any sign convention (rotated or unrotated camera), and
        self-calibrates its own px-per-mm for each marker.

        Runs after _filter_false_positives so we only refine markers
        that survived the bit-flip / ghost filter. Failures are
        non-fatal — if a refinement fails the interpolated value is
        kept unchanged.
        """
        if not self.discovered_bins:
            return

        feedrate = 6000           # moderate — accuracy, not speed
        PROBE_STEP_MM = 3.0        # baseline offset for slope measurement
        MAX_CORRECTION_MM = 20.0   # safety cap on computed correction

        # Sort by carriage X so we traverse bins in physical order,
        # minimising total travel distance.
        sorted_mids = sorted(
            self.discovered_bins.keys(),
            key=lambda m: self.discovered_bins[m].get(
                'carriage_x_centered', 0.0))

        total = len(sorted_mids)
        self.message = f'Refining {total} bin positions...'
        self.emit('calibration_progress', self._get_status())
        self.emit('log_message', {
            'message': f'Starting bin refinement pass ({total} markers)',
        })

        refined_count = 0
        failed_count = 0

        def _drive_and_settle(x):
            x_clamped = max(0.0, min(float(x), float(self.max_sweep_x)))
            try:
                gcode_module._send_and_wait(
                    f"G0 X{x_clamped:.2f} F{feedrate}")
                gcode_module._send_and_wait("M400")
            except Exception as e:
                print(f"[calibration] refine move failed: {e}")
                return None
            time.sleep(0.25)  # settle
            return x_clamped

        for idx, mid in enumerate(sorted_mids):
            if not self.running:
                self.emit('log_message', {
                    'message': 'Refinement cancelled',
                })
                return

            info = self.discovered_bins[mid]
            start_x = float(info.get('carriage_x_centered', 0.0))
            mtype = info.get('type', 'unknown')

            # Step 1: drive to interpolated center
            pos_a = _drive_and_settle(start_x)
            if pos_a is None:
                failed_count += 1
                continue

            # Step 2: first pixel offset measurement
            off_a = self._read_marker_offset(camera, mid, samples=5)
            if off_a is None:
                failed_count += 1
                self.emit('log_message', {
                    'message': (f'Refine: marker {mid} not visible at '
                                f'interpolated X={start_x:.2f} — '
                                f'keeping interpolated position'),
                })
                continue

            # Step 3: drive PROBE_STEP forward (don't go past sweep max)
            probe_x = start_x + PROBE_STEP_MM
            if probe_x > float(self.max_sweep_x) - 0.5:
                probe_x = start_x - PROBE_STEP_MM  # fall back to -direction
                effective_step = -PROBE_STEP_MM
            else:
                effective_step = PROBE_STEP_MM
            pos_b = _drive_and_settle(probe_x)
            if pos_b is None:
                failed_count += 1
                continue

            # Step 4: second pixel offset measurement
            off_b = self._read_marker_offset(camera, mid, samples=5)
            if off_b is None:
                # Marker drifted out of view. Fall back to pos_a as best.
                info['carriage_x_centered'] = float(pos_a)
                info['bin_x'] = float(pos_a) + self.camera_x_offset
                info['refined'] = False
                self.emit('log_message', {
                    'message': (f'Refine: marker {mid} lost after probe '
                                f'step, keeping X={pos_a:.2f}'),
                })
                failed_count += 1
                continue

            # Step 5: compute the px-per-mm slope empirically. Sign is
            # whatever it is — we trust the measurement.
            slope = (off_b - off_a) / float(effective_step)
            if abs(slope) < 0.5:
                # Not enough pixel change for a reliable fit —
                # marker probably sits at the edge of the frame or
                # the detector is noisy. Use pos_a.
                info['carriage_x_centered'] = float(pos_a)
                info['bin_x'] = float(pos_a) + self.camera_x_offset
                info['refined'] = False
                info['refine_slope'] = round(slope, 4)
                self.emit('log_message', {
                    'message': (f'Refine: marker {mid} slope too small '
                                f'({slope:.3f} px/mm), keeping '
                                f'X={pos_a:.2f}'),
                })
                failed_count += 1
                continue

            # Step 6: solve for zero crossing.
            # Line through (pos_a, off_a) and (pos_b, off_b).
            # Zero crossing at pos_a - off_a / slope.
            correction = -off_a / slope
            if abs(correction) > MAX_CORRECTION_MM:
                # Sanity clamp. If the two points are nearly collinear
                # and the extrapolated zero is absurdly far away,
                # something is wrong — bail to pos_a.
                info['carriage_x_centered'] = float(pos_a)
                info['bin_x'] = float(pos_a) + self.camera_x_offset
                info['refined'] = False
                info['refine_slope'] = round(slope, 4)
                self.emit('log_message', {
                    'message': (f'Refine: marker {mid} correction '
                                f'{correction:+.1f}mm exceeds safety '
                                f'cap, keeping X={pos_a:.2f}'),
                })
                failed_count += 1
                continue

            target_x = pos_a + correction
            final_x = _drive_and_settle(target_x)
            if final_x is None:
                failed_count += 1
                continue

            # Step 7: verification pass. One more offset read; if still
            # off by more than tolerance, apply a single additional
            # correction using the same slope.
            residual = self._read_marker_offset(camera, mid, samples=5)
            if residual is not None and abs(residual) > FINE_CENTER_TOLERANCE:
                extra_corr = -residual / slope
                if abs(extra_corr) <= MAX_CORRECTION_MM:
                    touched = _drive_and_settle(final_x + extra_corr)
                    if touched is not None:
                        final_x = touched
                        residual2 = self._read_marker_offset(
                            camera, mid, samples=5)
                        if residual2 is not None:
                            residual = residual2

            delta = final_x - start_x
            info['carriage_x_centered'] = float(final_x)
            info['bin_x'] = float(final_x) + self.camera_x_offset
            info['refined'] = True
            info['refine_delta_mm'] = round(delta, 3)
            info['refine_slope_px_per_mm'] = round(slope, 3)
            info['refine_residual_px'] = (round(residual, 1)
                                           if residual is not None else None)
            refined_count += 1

            self.emit('log_message', {
                'message': (f'Refined marker {mid} ({mtype}): '
                            f'X={final_x:.2f}mm '
                            f'(\u0394{delta:+.2f}mm, '
                            f'slope={slope:+.2f}px/mm, '
                            f'resid={residual:+.1f}px'
                            if residual is not None else
                            f'Refined marker {mid} ({mtype}): '
                            f'X={final_x:.2f}mm '
                            f'(\u0394{delta:+.2f}mm, '
                            f'slope={slope:+.2f}px/mm)'),
            })
            self.emit('calibration_marker_refined', {
                'marker_id': int(mid),
                'bin_x': round(info['bin_x'], 2),
                'carriage_x': round(final_x, 2),
                'delta_mm': round(delta, 3),
                'slope_px_per_mm': round(slope, 3),
                'residual_px': (round(residual, 1)
                                if residual is not None else None),
            })

            self.message = (f'Refined {idx + 1}/{total} bins '
                            f'(last \u0394={delta:+.2f}mm)')
            self.emit('calibration_progress', self._get_status())

        self.message = (f'Refinement done: {refined_count} refined, '
                        f'{failed_count} skipped')
        self.emit('log_message', {'message': self.message})
        self.emit('calibration_progress', self._get_status())

    def _fine_center_on_marker(self, camera, gcode_module, marker_id,
                                start_x, initial_frame, feedrate):
        """
        Nudge X position until the marker center is within tolerance of frame center.
        Returns the final X position, or None if centering failed.
        """
        current_x = start_x
        frame_w = initial_frame.shape[1]
        frame_center_x = frame_w // 2

        for iteration in range(FINE_CENTER_MAX_ITERATIONS):
            try:
                camera.flush_buffer()
            except Exception:
                pass
            frame = camera.get_frame()
            if frame is None:
                time.sleep(0.1)
                continue

            markers = self.detect_markers(frame)
            target = None
            for m in markers:
                if m['id'] == marker_id:
                    target = m
                    break

            if target is None:
                # Lost the marker — try small adjustments
                if iteration > 5:
                    return None
                time.sleep(0.2)
                continue

            cx, cy = target['center']
            offset_px = cx - frame_center_x

            if abs(offset_px) <= FINE_CENTER_TOLERANCE:
                # Centered!
                return current_x

            # Calculate X nudge: positive offset = marker is right of center
            # If camera moves right (+X), marker appears to move left in frame
            # So move in the direction of the offset
            nudge_mm = FINE_CENTER_STEP if offset_px > 0 else -FINE_CENTER_STEP

            # Scale nudge by how far off we are (proportional control)
            if abs(offset_px) > 50:
                nudge_mm *= 2.0
            elif abs(offset_px) > 100:
                nudge_mm *= 3.0

            current_x += nudge_mm
            current_x = max(0, min(current_x, self.max_sweep_x))

            gcode_module._send_and_wait(f"G0 X{current_x:.1f} F{feedrate}")
            gcode_module._send_and_wait("M400")
            time.sleep(0.15)

        # Failed to center after max iterations
        return current_x  # Return best effort

    def _build_bin_config(self):
        """
        Convert discovered markers into a bin configuration.

        Source bins get bin numbers 0, -1, -2, ... (0 is primary source)
        Destination bins get numbers 1, 2, 3, ... assigned left-to-right by X position.
        Staging platform gets its own entry if detected.
        """
        sources = []
        dests = []
        staging = None

        for mid, info in self.discovered_bins.items():
            if info['type'] == 'source':
                sources.append(info)
            elif info['type'] == 'dest':
                dests.append(info)
            elif info['type'] == 'staging':
                staging = info

        # Sort by X position (left to right)
        sources.sort(key=lambda b: b['bin_x'])
        dests.sort(key=lambda b: b['bin_x'])

        bin_config = {
            'source_bins': [],
            'dest_bins': [],
            'locations': {},  # {bin_number: x_position} — compatible with gcode_control
        }

        # Assign source bin numbers: 0 for primary, additional sources keep marker ID
        for i, src in enumerate(sources):
            bin_num = 0 if i == 0 else -(i)  # 0, -1, -2, etc.
            src['bin_number'] = bin_num
            bin_config['source_bins'].append({
                'bin_number': bin_num,
                'marker_id': src['marker_id'],
                'x': round(src['bin_x'], 1),
            })
            bin_config['locations'][str(bin_num)] = round(src['bin_x'], 1)

        # Assign destination bin numbers: 1, 2, 3, ... left to right
        for i, dest in enumerate(dests):
            bin_num = i + 1
            dest['bin_number'] = bin_num
            bin_config['dest_bins'].append({
                'bin_number': bin_num,
                'marker_id': dest['marker_id'],
                'x': round(dest['bin_x'], 1),
            })
            bin_config['locations'][str(bin_num)] = round(dest['bin_x'], 1)

        # Staging platform
        if staging is not None:
            bin_config['staging'] = {
                'marker_id': staging['marker_id'],
                'x': round(staging['bin_x'], 1),
            }

        bin_config['total_source'] = len(sources)
        bin_config['total_dest'] = len(dests)
        bin_config['total'] = len(sources) + len(dests) + (1 if staging else 0)

        return bin_config

    def check_bin_empty(self, camera, gcode_module, bin_x, bin_marker_id=None):
        """
        Check if a bin is empty by looking for its ArUco marker.
        The marker is on the bin floor — visible only when no cards are covering it.

        Moves the carriage so the camera is over the bin, then checks for the marker.

        `bin_x` is the marker's world X (== head target). To center the camera
        over it, the carriage must go to `bin_x - CAMERA_X_OFFSET`.
        (See camera offset convention at the top of this file.)

        Returns:
            True if bin is empty (marker visible)
            False if bin has cards (marker hidden)
            None if check failed
        """
        carriage_target = bin_x - self.camera_x_offset

        # Move camera over the bin
        gcode_module._send_and_wait(f"G0 X{carriage_target:.1f} F{gcode_module.X_FEEDRATE}")
        gcode_module._send_and_wait("M400")
        time.sleep(0.3)

        # Check multiple frames for reliability
        detections = 0
        checks = 5
        for _ in range(checks):
            frame = camera.get_frame()
            if frame is None:
                time.sleep(0.1)
                continue
            markers = self.detect_markers(frame)
            for m in markers:
                if bin_marker_id is not None:
                    if m['id'] == bin_marker_id:
                        detections += 1
                        break
                else:
                    # Accept any marker at this position
                    detections += 1
                    break
            time.sleep(0.05)

        # Marker seen in majority of frames = bin is empty
        is_empty = detections >= (checks // 2 + 1)
        return is_empty

    def check_source_bins_empty(self, camera, gcode_module, source_bins):
        """
        Check all source bins for emptiness.

        Args:
            source_bins: list of {'bin_number': int, 'marker_id': int, 'x': float}

        Returns:
            dict: {bin_number: bool}  True = empty
        """
        results = {}
        for src in source_bins:
            is_empty = self.check_bin_empty(
                camera, gcode_module,
                bin_x=src['x'],
                bin_marker_id=src['marker_id'],
            )
            results[src['bin_number']] = is_empty
            self.emit('bin_empty_check', {
                'bin_number': src['bin_number'],
                'marker_id': src['marker_id'],
                'x': src['x'],
                'empty': is_empty,
            })
        self.empty_bins.update(results)
        return results

    def get_nearest_source_bin(self, current_x, source_bins, empty_bins=None):
        """
        Find the closest non-empty source bin to the current X position.

        Used for dual-source optimization: after dropping a card, pick from
        whichever source bin is closer and still has cards.

        Args:
            current_x: current carriage X position
            source_bins: list of {'bin_number': int, 'x': float, ...}
            empty_bins: dict {bin_number: bool}, if None all bins assumed non-empty

        Returns:
            Best source bin dict, or None if all sources are empty
        """
        if empty_bins is None:
            empty_bins = {}

        candidates = [
            s for s in source_bins
            if not empty_bins.get(s['bin_number'], False)
        ]

        if not candidates:
            return None

        # Sort by distance from current position
        candidates.sort(key=lambda s: abs(s['x'] - current_x))
        return candidates[0]

    def calibrate_camera_offset(self, camera, gcode_module):
        """
        Semi-automatic camera offset calibration.

        1. User places a marker directly under the suction head
        2. User manually centers the suction head over the marker
        3. This function reads the marker position in the frame
        4. The pixel offset from center is converted to mm offset

        Returns the calibrated offset in mm, or None on failure.
        """
        frame = camera.get_frame()
        if frame is None:
            return None

        markers = self.detect_markers(frame)
        if not markers:
            return None

        # Use the first detected marker
        m = markers[0]
        cx, cy = m['center']
        frame_w = frame.shape[1]
        frame_center_x = frame_w // 2

        # Pixel offset from center
        px_offset = cx - frame_center_x

        # Convert pixels to mm — need to know the field of view
        # This is approximate; user should verify with a test move
        # Assuming ~200mm field of view across the frame width (adjust for your camera)
        mm_per_pixel = 200.0 / frame_w  # Rough estimate
        offset_mm = px_offset * mm_per_pixel

        return offset_mm

    def _get_status(self):
        """Get current calibration status."""
        return {
            'running': self.running,
            'progress': self.progress,
            'total_steps': self.total_steps,
            'message': self.message,
            'bins_found': len(self.discovered_bins),
            'discovered': [
                {
                    'marker_id': mid,
                    'type': info['type'],
                    'bin_x': round(info['bin_x'], 1),
                }
                for mid, info in sorted(self.discovered_bins.items(),
                                         key=lambda x: x[1]['bin_x'])
            ],
        }

    def get_status(self):
        """Thread-safe status getter."""
        with self._lock:
            return self._get_status()

    def cancel(self):
        """Cancel a running calibration."""
        self.running = False
        self.message = 'Cancelling...'


# Module-level singleton
calibrator = BinCalibrator()
