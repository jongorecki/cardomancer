# gcode_control.py
# --------------------------------------------------------------------------
# Manages the serial connection to the control board and provides G-code
# sequences for the card sorting process.
#
# Motion system:
#   X axis — horizontal carriage movement across bins
#   Z axis — suction head moves DOWN from home (top) into bins
#   Z homes at max (top), Z=0 is fully extended down
#   Z_MIN endstop = card/bin contact, Z_MAX endstop = home
# --------------------------------------------------------------------------

import serial
import time
import os
import sys

# =========================================================================
# TUNABLE PARAMETERS — adjust these to match your physical setup
# =========================================================================

SERIAL_PORT     = 'COM3'
BAUD_RATE       = 250000       # Matches Marlin BAUDRATE setting

# Feedrates (mm/min)
Z_FEEDRATE      = 8000         # Z travel speed (133mm/s)
Z_PROBE_FEEDRATE = 3600        # Z probe speed (60mm/s) — kept slow for accuracy
X_FEEDRATE      = 12000        # X travel speed (200mm/s)

# X positions (mm)
X_DETECTION_POSITION = 100.0   # X position for camera detection (offset from source bin)
X_SOURCE_BIN = 50.0            # X position of the source bin (offset from endstop to avoid snag)
X_STAGING_POSITION = 162.0     # X position of the white staging surface for detection
X_CAMERA_PARK = 50.0           # Where carriage parks during camera capture (out of camera view)
X_CAMERA_POSITION = 138.0     # X position for on-carriage camera to see staging area (recomputed by set_machine_positions)
# Carriage -> camera offset in mm. Positive = camera is to the right of
# the suction head. This gets overwritten by `set_machine_positions(
# camera_x_offset=...)` when the new hardware setup applies its results.
# The default 100.0 matches BinCalibrator's CAMERA_X_OFFSET default.
_camera_x_offset = 100.0
Z_CAMERA_POSITION = 204.0     # Z height for on-carriage camera to see staging area

# Z positions (mm) — Z=max is home (top), Z=0 is fully extended
Z_CLEAR_HEIGHT  = 200.0        # Z position to clear bin walls during X travel.
                                # Set to the minimum safe height that clears bin lips.
                                # Lower = faster. Measure your setup and adjust.
Z_MAX           = 220.0        # Z_MAX_POS from Marlin config. Z=220 is top (home), Z=0 is fully down.
Z_DROP_OFFSET   = 50.0         # How far below the top to lower when dropping a card.
                                # Drop position = Z_MAX - Z_DROP_OFFSET.
                                # Tune this so the suction head is just below the bin lip.

# Safe machine envelope — all high-level moves should be clamped to
# these bounds so a bad config, stale probe cache, or buggy calculation
# can't drive the carriage into an endstop at speed.
X_SAFE_MIN = 0.0
# X_SAFE_MAX is a runaway-value guard (catches e.g. X1000000 from a buffer
# bug), NOT the physical travel limit — that's enforced by Marlin's
# software endstops. Setting it too low causes calibration sweeps and
# fine-centering nudges to silently dead-end at the ceiling. Default
# covers machines up to 2m of travel; the UI's Max sweep X goes up to
# 2000mm so this matches. Raise via set_x_safe_max() if needed.
X_SAFE_MAX = 2000.0
Z_SAFE_MIN = 0.0
Z_SAFE_MAX = Z_MAX


def set_x_safe_max(value):
    """
    Update the X safety ceiling at runtime. Called by the calibrator
    before a sweep so a custom Max Sweep X in the UI automatically
    relaxes the clamp to match — otherwise we'd silently throttle
    every command past the default ceiling.

    Only ever grows the ceiling, never shrinks it. Shrinking here was
    a nasty trap: the calibrator would call this with
    max_sweep_x + margin (e.g. 940mm) and silently pull the ceiling
    DOWN from the 2000mm default, then the post-calibration probe
    phase would silently clamp far-bin moves back to 940mm — which
    looked exactly like "doesn't probe bins past a certain X".
    """
    global X_SAFE_MAX
    try:
        v = float(value)
    except (TypeError, ValueError):
        return
    # Ignore anything below a sanity floor.
    if v < 500.0:
        return
    old = X_SAFE_MAX
    if v <= old:
        # Never shrink. A caller who wants 940mm when we already allow
        # 2000mm gets no-op — the more-permissive ceiling stays.
        return
    X_SAFE_MAX = v
    print(f"[gcode] X_SAFE_MAX: {old:.0f} -> {v:.0f}")


def clamp_x(x):
    """Clamp an X target to the safe machine envelope."""
    try:
        xf = float(x)
    except (TypeError, ValueError):
        print(f"[gcode] clamp_x: invalid value {x!r}, defaulting to X_SAFE_MIN")
        return X_SAFE_MIN
    if xf < X_SAFE_MIN:
        print(f"[gcode] clamp_x: {xf} < {X_SAFE_MIN}, clamping")
        return X_SAFE_MIN
    if xf > X_SAFE_MAX:
        print(f"[gcode] clamp_x: {xf} > {X_SAFE_MAX}, clamping")
        return X_SAFE_MAX
    return xf


def clamp_z(z):
    """Clamp a Z target to the safe machine envelope."""
    try:
        zf = float(z)
    except (TypeError, ValueError):
        print(f"[gcode] clamp_z: invalid value {z!r}, defaulting to Z_SAFE_MAX")
        return Z_SAFE_MAX
    if zf < Z_SAFE_MIN:
        print(f"[gcode] clamp_z: {zf} < {Z_SAFE_MIN}, clamping")
        return Z_SAFE_MIN
    if zf > Z_SAFE_MAX:
        print(f"[gcode] clamp_z: {zf} > {Z_SAFE_MAX}, clamping")
        return Z_SAFE_MAX
    return zf

# Timing (milliseconds)
VACUUM_ON_DELAY_MS  = 250      # Wait after vacuum on before lifting (let suction grip)
PRESSURE_ON_MS      = 500      # How long to pulse pressure to release card

# Bin width (mm)
BIN_WIDTH = 100.0

# Bin fullness detection
# Z threshold: if probed Z at destination bin is above this, the bin is getting full.
# This is measured from the bottom — a higher probed Z = more cards stacked.
# Z_MAX - Z_DROP_OFFSET is the normal drop height. If probed Z is within
# BIN_FULL_MARGIN mm of the drop height, the bin is considered full.
BIN_FULL_MARGIN = 15.0  # mm — warn when stack is within this distance of drop height
_bin_fullness_callback = None  # Set by web_worker to receive fullness warnings

# Default bin X-locations (mm) for the standard sorter setup.
# Physical X travel is ~800mm. Bin 0 = source bin (pickup); the remaining
# bins are destination bins spaced BIN_WIDTH apart on the other side of
# the staging platform. Override with configure_bins() or set_bin_locations().
DEFAULT_BIN_X_LOCATIONS = {
    0: 50.0,     # Source bin (pickup) — offset from endstop
    1: 150.0,
    2: 250.0,
    3: 350.0,
    4: 450.0,
    5: 550.0,
    6: 650.0,
    7: 750.0,
}

# =========================================================================
# Bin configuration
# =========================================================================

_active_bin_locations = None


def configure_bins(bin_count, start_x=None, spacing=None):
    """
    Generate evenly-spaced bin locations for a given bin count.
    Bin 0 is always the source/pickup bin at X_SOURCE_BIN.
    """
    global _active_bin_locations
    if start_x is None:
        start_x = BIN_WIDTH  # First sort bin starts one bin-width from source
    if spacing is None:
        spacing = BIN_WIDTH
    _active_bin_locations = {0: X_SOURCE_BIN}
    for i in range(1, bin_count + 1):
        _active_bin_locations[i] = start_x + (i - 1) * spacing
    print(f"[gcode] Configured {bin_count} bins "
          f"(X={start_x} to {start_x + (bin_count - 1) * spacing})")


def get_bin_locations():
    """Return the active bin location mapping."""
    return _active_bin_locations or DEFAULT_BIN_X_LOCATIONS


def set_bin_locations(locations):
    """Set bin locations from a dict {bin_number: x_position}.
    Allows manual per-bin positioning.
    """
    global _active_bin_locations
    _active_bin_locations = {int(k): float(v) for k, v in locations.items()}
    print(f"[gcode] Set {len(_active_bin_locations)} bin locations manually")


STAGING_WIDTH = 200.0              # Width of staging platform in mm


def set_machine_positions(source_x=None, detection_x=None, staging_x=None,
                          staging_width=None, camera_x_offset=None):
    """Override machine reference positions (source bin, camera, staging surface).

    When `staging_x` (and optionally `camera_x_offset`) are updated, this
    also recomputes `X_CAMERA_POSITION` so that `move_to_camera_position()`
    centers the carriage-mounted camera over the calibrated staging
    surface. Previously X_CAMERA_POSITION was a hardcoded legacy value
    that didn't move with calibration — the direct cause of silent
    sort failures after a new hardware setup (the camera was looking
    at empty space so no card was ever found).
    """
    global X_SOURCE_BIN, X_DETECTION_POSITION, X_STAGING_POSITION, STAGING_WIDTH
    global X_CAMERA_POSITION, _camera_x_offset
    if source_x is not None:
        X_SOURCE_BIN = float(source_x)
        # Also update bin 0 in active locations
        if _active_bin_locations is not None:
            _active_bin_locations[0] = X_SOURCE_BIN
        print(f"[gcode] Source bin X = {X_SOURCE_BIN}")
    if detection_x is not None:
        X_DETECTION_POSITION = float(detection_x)
        print(f"[gcode] Detection position X = {X_DETECTION_POSITION}")
    if staging_x is not None:
        X_STAGING_POSITION = float(staging_x)
        print(f"[gcode] Staging position X = {X_STAGING_POSITION}")
    if camera_x_offset is not None:
        _camera_x_offset = float(camera_x_offset)
        print(f"[gcode] Camera X offset = {_camera_x_offset}")
    # Recompute the camera-over-staging carriage position whenever
    # either staging_x or camera_x_offset changes. The carriage must
    # move to (staging_x - camera_x_offset) to center the camera
    # above the staging surface.
    if staging_x is not None or camera_x_offset is not None:
        try:
            new_cam_x = max(0.0, X_STAGING_POSITION - _camera_x_offset)
            X_CAMERA_POSITION = float(new_cam_x)
            print(f"[gcode] Camera view carriage X = {X_CAMERA_POSITION} "
                  f"(staging {X_STAGING_POSITION} - offset {_camera_x_offset})")
        except Exception as e:
            print(f"[gcode] Failed to update X_CAMERA_POSITION: {e}")
    if staging_width is not None:
        STAGING_WIDTH = float(staging_width)
        print(f"[gcode] Staging width = {STAGING_WIDTH}")


# Keep backward-compatible alias
BIN_X_LOCATIONS = DEFAULT_BIN_X_LOCATIONS


def set_bin_fullness_callback(callback):
    """Set a callback function(bin_number, probed_z, drop_z) for fullness warnings."""
    global _bin_fullness_callback
    _bin_fullness_callback = callback


def _check_bin_fullness(bin_number, target_x):
    """
    Mini-probe at the destination bin before dropping.
    If the stack is close to the drop height, call the fullness callback.
    Returns the probed Z, or None if probe wasn't done.
    """
    drop_z = Z_MAX - Z_DROP_OFFSET
    # Probe down at the target position (we're already at target_x)
    _probe_with_cache(target_x)
    probed_z = _get_current_z()
    if probed_z is not None and _bin_fullness_callback:
        # probed_z is in absolute coords — higher Z = closer to home (top)
        # A full bin has cards stacked high = probed Z is high
        # If probed_z >= drop_z - BIN_FULL_MARGIN, bin is getting full
        if probed_z >= drop_z - BIN_FULL_MARGIN:
            _bin_fullness_callback(bin_number, probed_z, drop_z)
    return probed_z


# =========================================================================
# G-code trace log
# Writes every serial send/receive line to logs/gcode_trace.log.
# This survives Python stdout buffering and Flask's TTY-less mode,
# so you always have a full diagnostic trail after a stuck event.
# =========================================================================

_GCODE_LOG_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
_GCODE_LOG_PATH = os.path.join(_GCODE_LOG_DIR, 'gcode_trace.log')
_gcode_log_fh   = None   # open file handle; None until first use


def _gcode_trace(msg: str):
    """Write msg to gcode_trace.log AND stdout (force-flushed).

    Called from every send/receive path so the log captures a complete
    record of all serial I/O regardless of Python buffering state.
    """
    global _gcode_log_fh
    # Ensure log directory exists
    if _gcode_log_fh is None:
        try:
            os.makedirs(_GCODE_LOG_DIR, exist_ok=True)
            _gcode_log_fh = open(_GCODE_LOG_PATH, 'a', encoding='utf-8',
                                 buffering=1)  # line-buffered
            ts = time.strftime('%Y-%m-%d %H:%M:%S')
            _gcode_log_fh.write(f'\n--- gcode_trace opened {ts} ---\n')
        except Exception:
            _gcode_log_fh = None  # stay silent if we can't open log

    ts = time.strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    # stdout — force flush so it appears immediately in terminal/journal
    print(line, flush=True)
    # file log
    if _gcode_log_fh is not None:
        try:
            _gcode_log_fh.write(line + '\n')
        except Exception:
            pass


def close_gcode_log():
    """Flush and close the trace log (called on shutdown)."""
    global _gcode_log_fh
    if _gcode_log_fh is not None:
        try:
            _gcode_log_fh.flush()
            _gcode_log_fh.close()
        except Exception:
            pass
        _gcode_log_fh = None


# =========================================================================
# Serial connection
# =========================================================================

ser = None

# Serial error callback — the worker thread registers one of these at
# startup so it can be notified when the port dies unexpectedly
# (USB unplug, cable fault, board reset). Signature: callback(str msg).
_serial_error_callback = None
# Monotonic counter of serial errors since start, useful for UI telemetry.
_serial_error_count = 0
# Last error message, readable via get_serial_error_info().
_last_serial_error = None


def set_serial_error_callback(callback):
    """Register a hook that fires on serial failures (thread-safe).

    The worker thread uses this to auto-pause the sort session and
    surface a clear error in the web UI instead of letting the failure
    bubble up as a silent 'sort does nothing' symptom.
    """
    global _serial_error_callback
    _serial_error_callback = callback


def get_serial_error_info():
    """Return a dict with current serial error telemetry."""
    return {
        'error_count': _serial_error_count,
        'last_error': _last_serial_error,
        'connected': is_connected(),
    }


def _handle_serial_error(context, exc):
    """Centralized serial-error handler.

    Records the error, closes the port so subsequent writes don't
    dogpile on a broken socket, and notifies any registered callback.
    Called from every low-level serial read/write path.
    """
    global ser, _serial_error_count, _last_serial_error
    _serial_error_count += 1
    _last_serial_error = f'{context}: {exc}'
    print(f"[gcode] SERIAL ERROR ({context}): {exc}")
    # Force-close the port so is_connected() reports False and no other
    # thread tries to re-use the dead socket.
    try:
        if ser is not None:
            ser.close()
    except Exception:
        pass
    ser = None
    # Notify the worker so it can halt the session and emit a UI event.
    if _serial_error_callback is not None:
        try:
            _serial_error_callback(_last_serial_error)
        except Exception as cb_err:
            print(f"[gcode] serial error callback failed: {cb_err}")


def connect_to_board():
    """Open serial connection to the control board."""
    global ser, _last_serial_error
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2)
        time.sleep(2)        # Give the board time to reset
        ser.flushInput()
        ser.flushOutput()
        _last_serial_error = None
        print(f"[gcode] Connected to {SERIAL_PORT} at {BAUD_RATE} baud.")
        # Disable Marlin's software endstops. The firmware's X_MAX_POS
        # is 800mm but our rail actually travels further, and Marlin
        # SILENTLY clips any G0 X>800 to X=800 while still responding
        # "ok" — meaning a probe at bin 6/7 would happily report Z for
        # the WRONG X position. Our X_SAFE_MAX clamp (set via the UI's
        # Max Sweep X) is the authoritative bound now.
        # M211 S0 turns off both min and max software endstops for all
        # axes. That's fine: Z is still bounded by the physical probe
        # trigger (G38.2 stops on contact), and X is bounded by our
        # own clamp_x() called on every command.
        try:
            ser.write(b"M211 S0\n")
            time.sleep(0.05)
            # Drain any response so it doesn't desync the first real
            # _send_and_wait() call.
            for _ in range(5):
                line = ser.readline().decode('utf-8', errors='replace').strip()
                if not line:
                    break
                _gcode_trace(f'[gcode] << {line}')
            print("[gcode] Disabled Marlin software endstops (M211 S0) "
                  "— X_SAFE_MAX is now the authoritative X bound.")
        except Exception as e:
            print(f"[gcode] WARNING: could not disable software endstops: {e}")
    except serial.SerialException as e:
        print(f"[gcode] ERROR: Could not open serial port {SERIAL_PORT}. {e}")
        _last_serial_error = f'connect: {e}'
        ser = None


def is_connected():
    """Check if the serial connection is open."""
    try:
        return (ser is not None) and ser.is_open
    except Exception:
        return False


def close_connection():
    """Close the serial port."""
    global ser
    if is_connected():
        try:
            ser.close()
        except Exception as e:
            print(f"[gcode] close error: {e}")
        print("[gcode] Connection closed.")
    ser = None


def _clamp_motion_command(command):
    """Defensively clamp X/Z in G0/G1 commands to the safe envelope.

    This is a last-line-of-defense clamp — every G-code move sent over
    serial passes through here. If a caller accidentally computes an
    out-of-bounds X or Z (bad probe, stale config, calibration bug),
    the clamp catches it before it reaches the board and potentially
    crashes into an endstop at speed. Non-motion commands pass through
    untouched.
    """
    import re
    stripped = command.strip()
    # Only rewrite absolute-mode linear moves. Skip relative (G91 sets
    # this flag, and relative moves like "G0 Z-999" for probing are
    # deliberately unbounded).
    if not re.match(r'^G0?[01]\b', stripped, re.IGNORECASE):
        return command

    def _replace(match):
        axis = match.group(1).upper()
        raw = match.group(2)
        try:
            val = float(raw)
        except ValueError:
            return match.group(0)
        if axis == 'X':
            clamped = clamp_x(val)
        elif axis == 'Z':
            clamped = clamp_z(val)
        else:
            return match.group(0)
        if abs(clamped - val) > 0.001:
            print(f"[gcode] SAFETY CLAMP: {axis}{val} -> {axis}{clamped}")
            return f'{axis}{clamped:g}'
        return match.group(0)

    return re.sub(r'([XZxz])(-?\d+(?:\.\d+)?)', _replace, stripped)


def _send_gcode(command):
    """Send a single line of G-code over serial.

    Returns True if the write succeeded, False otherwise. Wraps all
    serial I/O in a try/except so a disconnect mid-sort no longer
    crashes the worker thread — the error is logged, the callback
    fires, and the command handler (via _send_and_wait's checking) can
    bail gracefully.

    Also clamps X/Z values in absolute-mode linear moves to the safe
    machine envelope as a last-line-of-defense against bad targets.
    """
    global ser
    if not is_connected():
        print("[gcode] WARNING: Not connected. Cannot send G-code.")
        return False
    # Defensive bounds clamp on absolute-mode motion commands.
    safe_command = _clamp_motion_command(command)
    cmd = safe_command.strip() + '\n'
    _gcode_trace(f'[gcode] >> {cmd.strip()}')
    try:
        ser.write(cmd.encode('utf-8'))
        return True
    except (serial.SerialException, OSError, AttributeError) as e:
        _handle_serial_error(f'write "{command}"', e)
        return False


def _read_response(max_lines=20):
    """Read response lines from the board until 'ok' or an error is seen.

    max_lines guards against infinite loops; default raised to 20 so that
    verbose Marlin output (echo:busy, probe position lines, etc.) between a
    command and its 'ok' doesn't desync the stream.  Each readline() call
    already has a 2-second timeout, so at most 40 s of blocking before we
    give up — in practice 'ok' arrives in well under 1 s for most commands.
    """
    global ser
    if not is_connected():
        return
    for _ in range(max_lines):
        try:
            line = ser.readline().decode('utf-8', errors='replace').strip()
        except (serial.SerialException, OSError, AttributeError) as e:
            _handle_serial_error('readline', e)
            return
        if not line:
            # Empty read = readline() timed out; keep waiting up to max_lines
            continue
        _gcode_trace(f'[gcode] << {line}')
        if line.startswith('ok') or 'rror' in line.lower():
            break
        # Marlin sends "echo:busy: processing" while a long move runs.
        # These are informational only — keep reading, don't break.
        if line.lower().startswith('echo:busy'):
            continue


def _send_and_wait(command):
    """Send a G-code line and read back the response."""
    _send_gcode(command)
    _read_response()


def _get_current_z():
    """Query current Z position via M114. Returns Z value or None."""
    xz = _get_current_xz()
    if xz is None:
        return None
    return xz[1]


def _get_current_x():
    """Query current X position via M114. Returns X value or None."""
    xz = _get_current_xz()
    if xz is None:
        return None
    return xz[0]


def _get_current_xz():
    """Query current X and Z positions via a single M114. Returns
    (x, z) tuple or None on failure. Using a single query is faster
    than calling _get_current_x() and _get_current_z() back-to-back
    and avoids double-draining the serial buffer."""
    if not is_connected():
        return None
    import re
    # Drain any leftover lines in the buffer first
    ser.timeout = 0.1
    while True:
        leftover = ser.readline().decode('utf-8', errors='replace').strip()
        if not leftover:
            break
        _gcode_trace(f'[gcode] << (drain) {leftover}')
    # Now send M114 and wait for the position response
    ser.timeout = 2.0
    _send_gcode("M114")
    for _ in range(30):
        line = ser.readline().decode('utf-8', errors='replace').strip()
        if not line:
            continue
        _gcode_trace(f'[gcode] << {line}')
        # M114 response looks like: X:50.00 Y:0.00 Z:150.00 E:0.00
        z_match = re.search(r'Z:\s*(-?[\d.]+)', line)
        x_match = re.search(r'X:\s*(-?[\d.]+)', line)
        if z_match and x_match:
            z_val = float(z_match.group(1))
            x_val = float(x_match.group(1))
            # IMPORTANT: consume the trailing "ok" from M114 so it doesn't
            # desync subsequent _send_and_wait calls
            for _ in range(10):
                ok_line = ser.readline().decode('utf-8', errors='replace').strip()
                if ok_line:
                    _gcode_trace(f'[gcode] << {ok_line}')
                if ok_line.startswith('ok'):
                    break
            return (x_val, z_val)
    return None


def verify_x_position(target_x, tolerance=0.5):
    """Query M114 and confirm the current X matches target_x within
    tolerance mm. Returns (actual_x, ok:bool). ok=False means the
    board accepted the move but didn't go there — almost always a
    silent firmware software-endstop clamp (Marlin X_MAX_POS). Log
    this loudly so a single misplaced probe doesn't poison the
    probe cache.
    """
    actual_x = _get_current_x()
    if actual_x is None:
        print(f"[gcode] verify_x_position: no M114 response (target {target_x})")
        return (None, False)
    delta = abs(actual_x - float(target_x))
    if delta > tolerance:
        print(f"[gcode] *** FIRMWARE SILENT CLAMP DETECTED *** "
              f"commanded X{target_x:.2f}, actually at X{actual_x:.2f} "
              f"(Δ{delta:.2f}mm). Marlin X_MAX_POS likely = {actual_x:.0f}. "
              f"Run M211 S0 to disable software endstops, or raise "
              f"X_MAX_POS in firmware.")
        return (actual_x, False)
    return (actual_x, True)


# =========================================================================
# Probe height cache — remembers last probed Z per X position
# =========================================================================

Z_APPROACH_MARGIN = 10.0  # mm above last known Z to fast-move before probing
_probe_z_cache = {}        # {x_position: last_probed_z}


def _probe_with_cache(x_position):
    """
    Probe down to contact, using cached Z height for fast approach.
    First time: probes from current Z (full travel).
    Subsequent times: fast-moves to (cached_z + margin), then probes slowly.
    Updates cache with new probe result.
    """
    cached_z = _probe_z_cache.get(x_position)
    if cached_z is not None:
        approach_z = cached_z + Z_APPROACH_MARGIN
        # Fast move to just above last known contact point
        _send_and_wait(f"G0 Z{approach_z} F{Z_FEEDRATE}")
        _send_and_wait("M400")

    # Probe down slowly to contact
    _send_and_wait("G91")
    _send_and_wait(f"G38.2 Z-999 F{Z_PROBE_FEEDRATE}")
    _send_and_wait("M400")  # Wait for probe to physically complete before querying position
    _send_and_wait("G90")

    # Query and cache the contact position
    z_pos = _get_current_z()
    if z_pos is not None:
        _probe_z_cache[x_position] = z_pos


def clear_probe_cache():
    """Clear cached probe heights (e.g. after homing or physical changes)."""
    _probe_z_cache.clear()


# =========================================================================
# Startup / homing
# =========================================================================

def home_all():
    """Home Z first (clear any bin), then X. Must be called before any movement."""
    _send_and_wait("G28 Z")
    _send_and_wait("G28 X")
    clear_probe_cache()
    print("[gcode] Homed: Z=max (top), X=0")


def home_x():
    """Home only the X axis to minimum."""
    _send_and_wait("G28 X")


def home_z():
    """Home only the Z axis to maximum (top)."""
    _send_and_wait("G28 Z")


def move_to_detection_position():
    """
    Move to the detection position: Z all the way up, then X offset
    so the carriage doesn't block the camera.
    """
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{Z_MAX} F{Z_FEEDRATE}")
    _send_and_wait("M400")  # Z must finish before X moves
    _send_and_wait(f"G0 X{X_DETECTION_POSITION} F{X_FEEDRATE}")
    _send_and_wait("M400")
    print("[gcode] At detection position")


# =========================================================================
# Basic motion
# =========================================================================

def move_x(x_pos):
    """Move X axis to an absolute position. Ensures Z is at top first."""
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{Z_MAX} F{Z_FEEDRATE}")
    _send_and_wait("M400")  # Z must be at top before any X move
    _send_and_wait(f"G0 X{x_pos} F{X_FEEDRATE}")


def move_z(z_pos):
    """Move Z axis to an absolute position."""
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{z_pos} F{Z_FEEDRATE}")


def z_to_top():
    """Move Z all the way to the top (Z=Z_MAX)."""
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{Z_MAX} F{Z_FEEDRATE}")


def z_probe_down():
    """
    Probe Z downward until Z_MIN endstop triggers (card contact).
    Uses G38.2 for controlled probing at a slower feedrate.
    Returns to absolute mode after probing.
    """
    _send_and_wait("G91")
    _send_and_wait(f"G38.2 Z-999 F{Z_PROBE_FEEDRATE}")
    _send_and_wait("G90")


# =========================================================================
# Vacuum / Pressure
# =========================================================================

def vacuum_on():
    """Turn on vacuum pump (FAN0)."""
    _send_and_wait("M106 P0 S255")


def vacuum_off():
    """Turn off vacuum pump (FAN0)."""
    _send_and_wait("M106 P0 S0")


def pressure_on():
    """Turn on pressure pump (FAN1)."""
    _send_and_wait("M106 P1 S255")


def pressure_off():
    """Turn off pressure pump (FAN1)."""
    _send_and_wait("M106 P1 S0")


def all_pumps_off():
    """Turn off both pumps. Safety function."""
    _send_and_wait("M106 P0 S0")
    _send_and_wait("M106 P1 S0")


# =========================================================================
# Card sorting sequence
# =========================================================================

def _z_travel_height():
    """Return the Z height to use for safe X travel."""
    return Z_CLEAR_HEIGHT if Z_CLEAR_HEIGHT is not None else Z_MAX


def pick_and_drop(bin_number):
    """
    Pick a card from the source bin and drop it in the target bin.
    Does NOT return to detection position — caller handles that.

    Z always completes before X moves to avoid hitting bins.
    Uses cached probe heights for fast approach.
    """
    if not is_connected():
        print("[gcode] Not connected. Attempting to connect...")
        connect_to_board()

    bin_locs = get_bin_locations()
    source_x = bin_locs.get(0, X_SOURCE_BIN)
    target_x = bin_locs.get(bin_number, 150.0)
    z_clear = _z_travel_height()
    drop_z = Z_MAX - Z_DROP_OFFSET

    _send_and_wait("G90")

    # 1) Z to clear height, wait, then X to source
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{source_x} F{X_FEEDRATE}")
    _send_and_wait("M400")

    # 2) Probe down to card contact (fast approach if cached)
    _probe_with_cache(source_x)

    # 3) Vacuum on, wait for grip
    _send_and_wait("M106 P0 S255")
    _send_and_wait(f"G4 P{VACUUM_ON_DELAY_MS}")

    # 4) Lift to clear height, wait, then travel to target
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{target_x} F{X_FEEDRATE}")
    _send_and_wait("M400")

    # 4b) Optional: mini-probe to check bin fullness
    _check_bin_fullness(bin_number, target_x)
    # Lift back to clear height after probe
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")

    # 5) Z lower to drop height
    _send_and_wait(f"G0 Z{drop_z} F{Z_FEEDRATE}")
    _send_and_wait("M400")

    # 6) Release card
    _send_and_wait("M106 P0 S0")
    _send_and_wait("M106 P1 S255")
    _send_and_wait(f"G4 P{PRESSURE_ON_MS}")
    _send_and_wait("M106 P1 S0")


def pick_from_source():
    """
    Pick a card from the source bin and lift to clear height.
    After this, the card is on the suction head at clear Z height above the source.
    Call deliver_to_bin() next to drop it in a target bin.
    """
    if not is_connected():
        connect_to_board()

    bin_locs = get_bin_locations()
    source_x = bin_locs.get(0, X_SOURCE_BIN)
    z_clear = _z_travel_height()

    _send_and_wait("G90")

    # Z to clear height, then X to source
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{source_x} F{X_FEEDRATE}")
    _send_and_wait("M400")

    # Probe down to card contact
    _probe_with_cache(source_x)

    # Vacuum on, wait for grip
    _send_and_wait("M106 P0 S255")
    _send_and_wait(f"G4 P{VACUUM_ON_DELAY_MS}")

    # Lift to clear height
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")

    print(f"[gcode] Card picked up from source at X={source_x}")


def deliver_to_bin(bin_number):
    """
    Deliver a card already on the suction head to the target bin,
    then return to detection position.
    Call pick_from_source() first.
    """
    bin_locs = get_bin_locations()
    target_x = bin_locs.get(bin_number, 150.0)
    z_clear = _z_travel_height()
    drop_z = Z_MAX - Z_DROP_OFFSET

    _send_and_wait("G90")

    # X to target bin
    _send_and_wait(f"G0 X{target_x} F{X_FEEDRATE}")
    _send_and_wait("M400")

    # Optional: mini-probe to check bin fullness
    _check_bin_fullness(bin_number, target_x)
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")

    # Z lower to drop height
    _send_and_wait(f"G0 Z{drop_z} F{Z_FEEDRATE}")
    _send_and_wait("M400")

    # Release card
    _send_and_wait("M106 P0 S0")
    _send_and_wait("M106 P1 S255")
    _send_and_wait(f"G4 P{PRESSURE_ON_MS}")
    _send_and_wait("M106 P1 S0")

    # Return to detection position
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{X_DETECTION_POSITION} F{X_FEEDRATE}")
    _send_and_wait("M400")

    print(f"[gcode] Card delivered to bin {bin_number}")


def return_to_detection_nonblocking():
    """
    Start returning to detection position without waiting for completion.
    Call wait_for_completion() later to block until the carriage arrives.
    Z lifts and completes before X moves.
    """
    z_clear = _z_travel_height()
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_gcode(f"G0 X{X_DETECTION_POSITION} F{X_FEEDRATE}")


def send_to_bin(bin_number, mode=None):
    """
    Pick a card from the source bin and deliver it to the target bin.
    Full sequence including return to detection position.

    Z always completes before X moves to avoid hitting bins.
    """
    print(f"[gcode] Sorting card into Bin {bin_number}")

    pick_and_drop(bin_number)

    # Return to detection position
    z_clear = _z_travel_height()
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{X_DETECTION_POSITION} F{X_FEEDRATE}")
    _send_and_wait("M400")

    print(f"[gcode] Card delivered to bin {bin_number}")


def move_to_bin(bin_number):
    """Move X to a bin position. Ensures Z is at top first."""
    x_target = get_bin_locations().get(bin_number, 150.0)
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{Z_MAX} F{Z_FEEDRATE}")
    _send_and_wait("M400")  # Z must be at top before any X move
    _send_and_wait(f"G0 X{x_target} F{X_FEEDRATE}")


# =========================================================================
# Staging motion (white background detection)
# =========================================================================

def pick_from_position(x_position):
    """
    Move to x_position, probe down to contact, vacuum grip, lift to clear height.
    Generic pick — works for source bin, staging area, or any surface.
    Uses cached probe height for fast approach on repeat visits.
    """
    z_clear = _z_travel_height()
    _send_and_wait("G90")
    # Lift to safe height first, then move X, then probe down
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{x_position} F{X_FEEDRATE}")
    _send_and_wait("M400")
    # Probe down to contact (fast approach if cached)
    _probe_with_cache(x_position)
    # Vacuum on, wait for grip
    _send_and_wait("M106 P0 S255")
    _send_and_wait(f"G4 P{VACUUM_ON_DELAY_MS}")
    # Lift
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")


def drop_on_surface(x_position):
    """
    Move to x_position, probe down to surface, release card, lift.
    Probes to surface first so card is placed precisely.
    Uses cached probe height for fast approach on repeat visits.
    """
    z_clear = _z_travel_height()
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{x_position} F{X_FEEDRATE}")
    _send_and_wait("M400")
    # Probe down to surface (fast approach if cached)
    _probe_with_cache(x_position)
    # Release card
    _send_and_wait("M106 P0 S0")
    _send_and_wait("M106 P1 S255")
    _send_and_wait(f"G4 P{PRESSURE_ON_MS}")
    _send_and_wait("M106 P1 S0")
    # Lift
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")


def quick_drop(x_position):
    """
    Move to x_position, lower to drop height, release card, lift.
    No probing — just drops from a fixed height. Fast for drop zones
    where precise placement isn't needed.
    """
    z_clear = _z_travel_height()
    drop_z = Z_MAX - Z_DROP_OFFSET
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{x_position} F{X_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 Z{drop_z} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    # Release card
    _send_and_wait("M106 P0 S0")
    _send_and_wait("M106 P1 S255")
    _send_and_wait(f"G4 P{PRESSURE_ON_MS}")
    _send_and_wait("M106 P1 S0")
    # Lift
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")


def park_for_camera():
    """Move carriage to park position so camera has a clear view of staging area."""
    z_clear = _z_travel_height()
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait(f"G0 X{X_CAMERA_PARK} F{X_FEEDRATE}")
    _send_and_wait("M400")


def move_to_camera_position():
    """
    Move carriage to the on-carriage camera viewing position.
    Z moves first (up to camera height), then X shifts to camera offset.
    """
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{Z_CAMERA_POSITION} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{X_CAMERA_POSITION} F{X_FEEDRATE}")
    _send_and_wait("M400")


# --- Smart staging: probe first N times, then use cached height ---

_staging_probe_count = 0
STAGING_PROBE_INITIAL = 3     # Probe this many times before trusting cache
STAGING_REPROBE_INTERVAL = 50  # Re-probe every N cards to catch drift


def drop_on_staging(card_number=None):
    """
    Drop card on staging area. Probes the first few times to establish
    the surface height, then uses cached height for speed.
    Re-probes every STAGING_REPROBE_INTERVAL cards.
    """
    global _staging_probe_count

    needs_probe = (
        _staging_probe_count < STAGING_PROBE_INITIAL
        or X_STAGING_POSITION not in _probe_z_cache
        or (card_number is not None
            and card_number % STAGING_REPROBE_INTERVAL == 0)
    )

    z_clear = _z_travel_height()
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{X_STAGING_POSITION} F{X_FEEDRATE}")
    _send_and_wait("M400")

    if needs_probe:
        _probe_with_cache(X_STAGING_POSITION)
        _staging_probe_count += 1
    else:
        # Use cached height — fast move directly to surface
        cached_z = _probe_z_cache[X_STAGING_POSITION]
        _send_and_wait(f"G0 Z{cached_z} F{Z_FEEDRATE}")
        _send_and_wait("M400")

    # Release card
    _send_and_wait("M106 P0 S0")
    _send_and_wait("M106 P1 S255")
    _send_and_wait(f"G4 P{PRESSURE_ON_MS}")
    _send_and_wait("M106 P1 S0")
    # Lift just enough to clear the card (don't go all the way up)
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")


def pick_from_staging():
    """
    Pick card from staging area:
    1. Move X to staging position (at safe Z height)
    2. Fast-move Z near cached surface height
    3. Probe down slowly to card contact
    4. Vacuum on, wait for grip
    5. Lift to travel height
    """
    z_clear = _z_travel_height()
    _send_and_wait("G90")
    # Move X first at safe height
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{X_STAGING_POSITION} F{X_FEEDRATE}")
    _send_and_wait("M400")

    # Fast approach near surface, then probe to card contact
    cached_z = _probe_z_cache.get(X_STAGING_POSITION)
    if cached_z is not None:
        # Fast-move to just above the cached surface height
        approach_z = cached_z + Z_APPROACH_MARGIN
        _send_and_wait(f"G0 Z{approach_z} F{Z_FEEDRATE}")
        _send_and_wait("M400")

    # Probe down to actual card contact
    _send_and_wait("G91")
    _send_and_wait(f"G38.2 Z-999 F{Z_PROBE_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait("G90")

    # Vacuum on, wait for grip
    _send_and_wait("M106 P0 S255")
    _send_and_wait(f"G4 P{VACUUM_ON_DELAY_MS}")
    # Lift to travel height
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")


# =========================================================================
# Synchronization
# =========================================================================

def wait_for_completion():
    """Block until all queued moves are finished."""
    if not is_connected():
        return
    _send_and_wait("M400")
