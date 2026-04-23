#!/usr/bin/env python3
"""
Motion Speed Benchmark for Card Sorter
---------------------------------------
Tests X and Z axis speeds at increasing feedrates to find the maximum
reliable speed for each axis. Detects stalls/lost steps via M114
position checks after every move.

Also profiles:
  - Short vs long moves (acceleration effects)
  - Full mock sort cycle timing at current vs optimized speeds
  - Firmware acceleration/jerk settings (M503)

Usage:
  python benchmark_motion.py          # Full benchmark
  python benchmark_motion.py --x      # X axis only
  python benchmark_motion.py --z      # Z axis only
  python benchmark_motion.py --cycle  # Full cycle timing only
  python benchmark_motion.py --info   # Just read firmware settings

IMPORTANT: Run with the machine clear of cards and bins accessible.
           The script moves across the full X and Z range.
"""

import os
import sys
import re
import time
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

from gcode_control import (
    connect_to_board, is_connected, close_connection,
    _send_gcode, _send_and_wait, _read_response, _get_current_xz,
    home_all, clear_probe_cache,
    X_SOURCE_BIN, X_STAGING_POSITION, X_CAMERA_POSITION,
    Z_MAX, Z_CLEAR_HEIGHT, Z_DROP_OFFSET,
    X_FEEDRATE, Z_FEEDRATE, Z_PROBE_FEEDRATE,
    VACUUM_ON_DELAY_MS, PRESSURE_ON_MS,
    ser,
)


# =========================================================================
# Helpers
# =========================================================================

POSITION_TOLERANCE = 1.0  # mm — positions within this are considered OK


def get_position():
    """Return (x, z) tuple from M114, or (None, None) on failure."""
    result = _get_current_xz()
    if result is None:
        return None, None
    return result


def timed_move(gcode_cmd, label="move"):
    """
    Send a G-code move, wait for completion (M400), return elapsed seconds.
    """
    t0 = time.perf_counter()
    _send_and_wait("G90")
    _send_and_wait(gcode_cmd)
    _send_and_wait("M400")
    elapsed = time.perf_counter() - t0
    return elapsed


def verify_position(expected_x=None, expected_z=None):
    """
    Check M114 position against expected values.
    Returns (actual_x, actual_z, ok: bool, error_desc: str).
    """
    x, z = get_position()
    if x is None:
        return None, None, False, "M114 returned no data"

    errors = []
    if expected_x is not None and abs(x - expected_x) > POSITION_TOLERANCE:
        errors.append(f"X off by {abs(x - expected_x):.1f}mm "
                      f"(expected {expected_x:.1f}, got {x:.1f})")
    if expected_z is not None and abs(z - expected_z) > POSITION_TOLERANCE:
        errors.append(f"Z off by {abs(z - expected_z):.1f}mm "
                      f"(expected {expected_z:.1f}, got {z:.1f})")

    if errors:
        return x, z, False, "; ".join(errors)
    return x, z, True, ""


def read_firmware_settings():
    """
    Send M503 and parse acceleration, jerk, and max feedrate settings.
    Returns a dict of parsed values.
    """
    import gcode_control
    s = gcode_control.ser
    if s is None or not s.is_open:
        return {}

    # Drain buffer
    s.timeout = 0.1
    while True:
        leftover = s.readline().decode('utf-8', errors='replace').strip()
        if not leftover:
            break

    s.timeout = 2.0
    _send_gcode("M503")

    settings = {}
    lines = []
    for _ in range(100):
        line = s.readline().decode('utf-8', errors='replace').strip()
        if not line:
            continue
        lines.append(line)
        if line.startswith('ok'):
            break

    for line in lines:
        # M201 - Max Acceleration (units/s2): X<val> Y<val> Z<val>
        if 'M201' in line and 'X' in line:
            m = re.findall(r'([XYZE])(\d+(?:\.\d+)?)', line)
            for axis, val in m:
                settings[f'max_accel_{axis}'] = float(val)

        # M203 - Max Feedrate (units/s): X<val> Y<val> Z<val>
        if 'M203' in line and 'X' in line:
            m = re.findall(r'([XYZE])(\d+(?:\.\d+)?)', line)
            for axis, val in m:
                settings[f'max_feedrate_{axis}'] = float(val)

        # M204 - Acceleration: P<print> R<retract> T<travel>
        if 'M204' in line:
            m = re.findall(r'([PRT])(\d+(?:\.\d+)?)', line)
            for key, val in m:
                label = {'P': 'print_accel', 'R': 'retract_accel',
                         'T': 'travel_accel'}[key]
                settings[label] = float(val)

        # M205 - Jerk: X<val> Z<val> (or J<junction_deviation>)
        if 'M205' in line:
            m = re.findall(r'([XYZEJ])(\d+(?:\.\d+)?)', line)
            for key, val in m:
                if key == 'J':
                    settings['junction_deviation'] = float(val)
                else:
                    settings[f'jerk_{key}'] = float(val)

    return settings


def print_header(title):
    """Print a section header."""
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def print_subheader(title):
    """Print a subsection header."""
    print()
    print(f"--- {title} ---")


# =========================================================================
# Benchmark: X Axis Speed
# =========================================================================

def benchmark_x_axis():
    """
    Test X axis at increasing feedrates.
    Moves between X=50 and X=750 (700mm travel), verifies position after each.
    """
    print_header("X AXIS SPEED BENCHMARK")

    x_start = 50.0
    x_end = 750.0
    travel_mm = x_end - x_start

    # Feedrate range: 6000 to 27000 mm/min in 3000 steps
    # (100 mm/s to 450 mm/s). Capped at 27000 because 30000-36000
    # showed intermittent position errors under our machine's mechanics.
    feedrates = list(range(6000, 28000, 3000))

    print(f"  Travel distance: {travel_mm:.0f}mm  (X={x_start} -> X={x_end})")
    print(f"  Testing {len(feedrates)} feedrates: "
          f"{feedrates[0]}-{feedrates[-1]} mm/min "
          f"({feedrates[0]/60:.0f}-{feedrates[-1]/60:.0f} mm/s)")
    print(f"  Current X_FEEDRATE: {X_FEEDRATE} mm/min ({X_FEEDRATE/60:.0f} mm/s)")
    print()
    print(f"  {'Feedrate':>10} {'Speed':>8} {'Time':>8} {'Actual':>8} "
          f"{'Pos OK':>6}  {'Notes'}")
    print(f"  {'(mm/min)':>10} {'(mm/s)':>8} {'(sec)':>8} {'(mm/s)':>8} "
          f"{'':>6}  {''}")
    print("  " + "-" * 65)

    # Start at x_start
    _send_and_wait("G90")
    timed_move(f"G0 X{x_start} F{X_FEEDRATE}")

    results = []
    last_good_feedrate = 0

    for fr in feedrates:
        speed_mms = fr / 60.0

        # Move out (start -> end)
        t_out = timed_move(f"G0 X{x_end} F{fr}")
        _, _, ok_out, err_out = verify_position(expected_x=x_end)

        # Move back (end -> start)
        t_back = timed_move(f"G0 X{x_start} F{fr}")
        _, _, ok_back, err_back = verify_position(expected_x=x_start)

        avg_time = (t_out + t_back) / 2.0
        actual_speed = travel_mm / avg_time if avg_time > 0 else 0

        ok = ok_out and ok_back
        notes = []
        if not ok_out:
            notes.append(f"OUT: {err_out}")
        if not ok_back:
            notes.append(f"BACK: {err_back}")
        if ok:
            last_good_feedrate = fr
            # Check if firmware capped the speed (actual much lower than requested)
            if actual_speed < speed_mms * 0.85:
                notes.append(f"firmware-capped")

        status = "OK" if ok else "FAIL"
        note_str = "; ".join(notes) if notes else ""

        print(f"  {fr:>10} {speed_mms:>8.0f} {avg_time:>8.2f} "
              f"{actual_speed:>8.1f} {status:>6}  {note_str}")

        results.append({
            'feedrate': fr,
            'speed_mms': speed_mms,
            'time': avg_time,
            'actual_speed': actual_speed,
            'ok': ok,
            'notes': note_str,
        })

        if not ok:
            print(f"\n  ** STALL DETECTED at {fr} mm/min ({speed_mms:.0f} mm/s) **")
            print(f"     Re-homing to recover...")
            home_all()
            timed_move(f"G0 X{x_start} F{X_FEEDRATE}")
            # Continue testing — the stall might be intermittent
            # But if two consecutive fails, stop
            if len(results) >= 2 and not results[-2]['ok']:
                print(f"  ** Two consecutive failures. Stopping X test. **")
                break

    print()
    print(f"  RESULT: Max reliable X feedrate = {last_good_feedrate} mm/min "
          f"({last_good_feedrate/60:.0f} mm/s)")
    if last_good_feedrate > X_FEEDRATE:
        pct = (last_good_feedrate - X_FEEDRATE) / X_FEEDRATE * 100
        print(f"          {pct:.0f}% faster than current setting ({X_FEEDRATE})")
    elif last_good_feedrate < X_FEEDRATE:
        print(f"          WARNING: Current setting ({X_FEEDRATE}) may be too fast!")

    return results, last_good_feedrate


# =========================================================================
# Benchmark: Z Axis Speed
# =========================================================================

def benchmark_z_axis():
    """
    Test Z axis at increasing feedrates.
    Moves between Z=220 (top) and Z=100, verifies position after each.
    Stays at X=50 (source bin) to avoid hitting anything.
    """
    print_header("Z AXIS SPEED BENCHMARK")

    z_top = Z_MAX       # 220
    z_bottom = 100.0     # Safe bottom — well above any bin contact
    travel_mm = z_top - z_bottom

    # Capped at 12000 — benchmarked max reliable. 14000 showed failures,
    # sweep stays below that line for regression runs.
    feedrates = list(range(4000, 13000, 2000))

    print(f"  Travel distance: {travel_mm:.0f}mm  (Z={z_top} -> Z={z_bottom})")
    print(f"  Testing {len(feedrates)} feedrates: "
          f"{feedrates[0]}-{feedrates[-1]} mm/min "
          f"({feedrates[0]/60:.0f}-{feedrates[-1]/60:.0f} mm/s)")
    print(f"  Current Z_FEEDRATE: {Z_FEEDRATE} mm/min ({Z_FEEDRATE/60:.0f} mm/s)")
    print()
    print(f"  {'Feedrate':>10} {'Speed':>8} {'Time':>8} {'Actual':>8} "
          f"{'Pos OK':>6}  {'Notes'}")
    print(f"  {'(mm/min)':>10} {'(mm/s)':>8} {'(sec)':>8} {'(mm/s)':>8} "
          f"{'':>6}  {''}")
    print("  " + "-" * 65)

    # Ensure we're at a safe X, Z at top
    _send_and_wait("G90")
    timed_move(f"G0 X{X_SOURCE_BIN} F{X_FEEDRATE}")
    timed_move(f"G0 Z{z_top} F{Z_FEEDRATE}")

    results = []
    last_good_feedrate = 0

    for fr in feedrates:
        speed_mms = fr / 60.0

        # Move down (top -> bottom)
        t_down = timed_move(f"G0 Z{z_bottom} F{fr}")
        _, _, ok_down, err_down = verify_position(expected_z=z_bottom)

        # Move up (bottom -> top)
        t_up = timed_move(f"G0 Z{z_top} F{fr}")
        _, _, ok_up, err_up = verify_position(expected_z=z_top)

        avg_time = (t_down + t_up) / 2.0
        actual_speed = travel_mm / avg_time if avg_time > 0 else 0

        ok = ok_down and ok_up
        notes = []
        if not ok_down:
            notes.append(f"DOWN: {err_down}")
        if not ok_up:
            notes.append(f"UP: {err_up}")
        if ok:
            last_good_feedrate = fr
            if actual_speed < speed_mms * 0.85:
                notes.append(f"firmware-capped")

        status = "OK" if ok else "FAIL"
        note_str = "; ".join(notes) if notes else ""

        print(f"  {fr:>10} {speed_mms:>8.0f} {avg_time:>8.2f} "
              f"{actual_speed:>8.1f} {status:>6}  {note_str}")

        results.append({
            'feedrate': fr,
            'speed_mms': speed_mms,
            'time': avg_time,
            'actual_speed': actual_speed,
            'ok': ok,
            'notes': note_str,
        })

        if not ok:
            print(f"\n  ** STALL DETECTED at {fr} mm/min ({speed_mms:.0f} mm/s) **")
            print(f"     Re-homing to recover...")
            home_all()
            if len(results) >= 2 and not results[-2]['ok']:
                print(f"  ** Two consecutive failures. Stopping Z test. **")
                break

    print()
    print(f"  RESULT: Max reliable Z feedrate = {last_good_feedrate} mm/min "
          f"({last_good_feedrate/60:.0f} mm/s)")
    if last_good_feedrate > Z_FEEDRATE:
        pct = (last_good_feedrate - Z_FEEDRATE) / Z_FEEDRATE * 100
        print(f"          {pct:.0f}% faster than current setting ({Z_FEEDRATE})")
    elif last_good_feedrate < Z_FEEDRATE:
        print(f"          WARNING: Current setting ({Z_FEEDRATE}) may be too fast!")

    return results, last_good_feedrate


# =========================================================================
# Benchmark: Acceleration Effects (short vs long moves)
# =========================================================================

def benchmark_acceleration():
    """
    Test how move distance affects actual speed.
    Short moves never reach top speed due to acceleration ramp.
    This shows where the breakeven is.
    """
    print_header("ACCELERATION EFFECTS (X axis)")

    feedrate = X_FEEDRATE
    distances = [10, 25, 50, 100, 200, 350, 500, 700]
    x_start = 50.0

    print(f"  Feedrate: {feedrate} mm/min ({feedrate/60:.0f} mm/s)")
    print(f"  Measuring actual speed vs distance to show accel ramp effect")
    print()
    print(f"  {'Distance':>10} {'Time':>8} {'Actual':>8} {'Efficiency':>10}  "
          f"{'Notes'}")
    print(f"  {'(mm)':>10} {'(sec)':>8} {'(mm/s)':>8} {'(%)':>10}  {''}")
    print("  " + "-" * 55)

    target_speed = feedrate / 60.0

    # Start at x_start
    _send_and_wait("G90")
    timed_move(f"G0 X{x_start} F{feedrate}")

    for dist in distances:
        x_end = x_start + dist

        # Move out
        t0 = time.perf_counter()
        _send_and_wait(f"G0 X{x_end} F{feedrate}")
        _send_and_wait("M400")
        t_out = time.perf_counter() - t0

        # Move back
        t0 = time.perf_counter()
        _send_and_wait(f"G0 X{x_start} F{feedrate}")
        _send_and_wait("M400")
        t_back = time.perf_counter() - t0

        avg_time = (t_out + t_back) / 2.0
        actual_speed = dist / avg_time if avg_time > 0 else 0
        efficiency = (actual_speed / target_speed * 100) if target_speed > 0 else 0

        notes = ""
        if efficiency < 50:
            notes = "accel-limited"
        elif efficiency < 80:
            notes = "partially accel-limited"

        print(f"  {dist:>10} {avg_time:>8.3f} {actual_speed:>8.1f} "
              f"{efficiency:>9.1f}%  {notes}")

    print()
    print("  Short moves never reach full speed — the motor spends the entire")
    print("  move accelerating then decelerating. This is normal. Focus speed")
    print("  tuning on the long moves (X travel between bins).")


# =========================================================================
# Benchmark: Full Sort Cycle Timing
# =========================================================================

def benchmark_sort_cycle(x_feedrate=None, z_feedrate=None, label=""):
    """
    Time a complete simulated sort cycle (no card, no vacuum).
    Measures each phase individually to show where time goes.

    The production sort cycle is:
      1. Pick from source: X->source, Z probe down, Z lift
      2. Drop on staging: X->staging, Z down, Z lift
      3. Camera capture: X->camera park (+ image capture time, not measured)
      4. Pick from staging: X->staging, Z probe down, Z lift
      5. Deliver to bin: X->target bin, Z drop height, Z lift
      6. Return to camera: X->camera position
    """
    if x_feedrate is None:
        x_feedrate = X_FEEDRATE
    if z_feedrate is None:
        z_feedrate = Z_FEEDRATE

    z_clear = Z_CLEAR_HEIGHT
    drop_z = Z_MAX - Z_DROP_OFFSET
    # Simulate bin at X=450 (middle-ish bin)
    target_bin_x = 450.0
    source_x = X_SOURCE_BIN
    staging_x = X_STAGING_POSITION
    camera_x = X_CAMERA_POSITION

    cycle_label = label or f"F{x_feedrate}/F{z_feedrate}"
    print_subheader(f"Sort Cycle: {cycle_label}")
    print(f"  X feedrate: {x_feedrate} mm/min ({x_feedrate/60:.0f} mm/s)")
    print(f"  Z feedrate: {z_feedrate} mm/min ({z_feedrate/60:.0f} mm/s)")
    print(f"  Target bin: X={target_bin_x}")
    print()

    _send_and_wait("G90")

    phases = []

    # Start at camera position (where a sort cycle begins)
    timed_move(f"G0 Z{z_clear} F{z_feedrate}")
    timed_move(f"G0 X{camera_x} F{x_feedrate}")

    # Phase 1: Move to source + Z down + Z up (simulated pick)
    t0 = time.perf_counter()
    _send_and_wait(f"G0 Z{z_clear} F{z_feedrate}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 X{source_x} F{x_feedrate}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 Z{drop_z} F{z_feedrate}")  # Simulate probe depth
    _send_and_wait("M400")
    # Simulate vacuum delay
    _send_and_wait(f"G4 P{VACUUM_ON_DELAY_MS}")
    _send_and_wait(f"G0 Z{z_clear} F{z_feedrate}")
    _send_and_wait("M400")
    t1 = time.perf_counter()
    phases.append(("Pick from source", t1 - t0))

    # Phase 2: Move to staging + Z down + release + Z up
    t0 = time.perf_counter()
    _send_and_wait(f"G0 X{staging_x} F{x_feedrate}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 Z{drop_z} F{z_feedrate}")
    _send_and_wait("M400")
    _send_and_wait(f"G4 P{PRESSURE_ON_MS}")  # Release delay
    _send_and_wait(f"G0 Z{z_clear} F{z_feedrate}")
    _send_and_wait("M400")
    t1 = time.perf_counter()
    phases.append(("Drop on staging", t1 - t0))

    # Phase 3: Move to camera position
    t0 = time.perf_counter()
    _send_and_wait(f"G0 X{camera_x} F{x_feedrate}")
    _send_and_wait("M400")
    t1 = time.perf_counter()
    phases.append(("Move to camera", t1 - t0))

    # (Camera capture + identification would happen here — ~500ms)
    phases.append(("Card ID (estimated)", 0.5))

    # Phase 4: Pick from staging
    t0 = time.perf_counter()
    _send_and_wait(f"G0 X{staging_x} F{x_feedrate}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 Z{drop_z} F{z_feedrate}")
    _send_and_wait("M400")
    _send_and_wait(f"G4 P{VACUUM_ON_DELAY_MS}")
    _send_and_wait(f"G0 Z{z_clear} F{z_feedrate}")
    _send_and_wait("M400")
    t1 = time.perf_counter()
    phases.append(("Pick from staging", t1 - t0))

    # Phase 5: Deliver to target bin
    t0 = time.perf_counter()
    _send_and_wait(f"G0 X{target_bin_x} F{x_feedrate}")
    _send_and_wait("M400")
    _send_and_wait(f"G0 Z{drop_z} F{z_feedrate}")
    _send_and_wait("M400")
    _send_and_wait(f"G4 P{PRESSURE_ON_MS}")
    _send_and_wait(f"G0 Z{z_clear} F{z_feedrate}")
    _send_and_wait("M400")
    t1 = time.perf_counter()
    phases.append(("Deliver to bin", t1 - t0))

    # Phase 6: Return to camera
    t0 = time.perf_counter()
    _send_and_wait(f"G0 X{camera_x} F{x_feedrate}")
    _send_and_wait("M400")
    t1 = time.perf_counter()
    phases.append(("Return to camera", t1 - t0))

    # Print results
    total = sum(t for _, t in phases)
    print(f"  {'Phase':<25} {'Time':>8}  {'% of cycle':>10}")
    print("  " + "-" * 48)
    for name, t in phases:
        pct = t / total * 100 if total > 0 else 0
        bar = "#" * int(pct / 2)
        print(f"  {name:<25} {t:>7.2f}s  {pct:>9.1f}%  {bar}")
    print("  " + "-" * 48)
    print(f"  {'TOTAL':<25} {total:>7.2f}s")
    print(f"  Cards per minute: {60.0 / total:.1f}")
    print(f"  Cards per hour:   {3600.0 / total:.0f}")

    return phases, total


# =========================================================================
# Benchmark: Repeatability
# =========================================================================

def benchmark_repeatability(n_cycles=5):
    """
    Run the same move N times and check position consistency.
    Tests whether the machine drifts over repeated cycles.
    """
    print_header(f"REPEATABILITY TEST ({n_cycles} cycles)")

    x_a = 50.0
    x_b = 700.0

    print(f"  Moving X={x_a} <-> X={x_b}, {n_cycles} round trips")
    print(f"  Checking position after each move")
    print()
    print(f"  {'Cycle':>5} {'X after A':>10} {'X after B':>10} {'Error A':>8} {'Error B':>8}")
    print("  " + "-" * 50)

    _send_and_wait("G90")
    timed_move(f"G0 X{x_a} F{X_FEEDRATE}")

    max_error = 0.0

    for i in range(1, n_cycles + 1):
        # Move to B
        timed_move(f"G0 X{x_b} F{X_FEEDRATE}")
        xb, _ = get_position()
        err_b = abs(xb - x_b) if xb is not None else -1

        # Move to A
        timed_move(f"G0 X{x_a} F{X_FEEDRATE}")
        xa, _ = get_position()
        err_a = abs(xa - x_a) if xa is not None else -1

        max_error = max(max_error, err_a, err_b)

        print(f"  {i:>5} {xa:>10.2f} {xb:>10.2f} "
              f"{err_a:>7.2f}mm {err_b:>7.2f}mm")

    print()
    if max_error <= POSITION_TOLERANCE:
        print(f"  PASS: Max position error = {max_error:.2f}mm (within tolerance)")
    else:
        print(f"  WARN: Max position error = {max_error:.2f}mm "
              f"(exceeds {POSITION_TOLERANCE}mm tolerance)")


# =========================================================================
# Main
# =========================================================================

def main():
    parser = argparse.ArgumentParser(description="Motion speed benchmark")
    parser.add_argument('--x', action='store_true', help="X axis benchmark only")
    parser.add_argument('--z', action='store_true', help="Z axis benchmark only")
    parser.add_argument('--cycle', action='store_true', help="Sort cycle timing only")
    parser.add_argument('--accel', action='store_true', help="Acceleration effects only")
    parser.add_argument('--repeat', action='store_true', help="Repeatability test only")
    parser.add_argument('--info', action='store_true', help="Read firmware settings only")
    args = parser.parse_args()

    run_all = not any([args.x, args.z, args.cycle, args.accel, args.repeat, args.info])

    # ------------------------------------------------------------------
    # Connect and read firmware settings
    # ------------------------------------------------------------------
    print_header("MOTION SPEED BENCHMARK")
    print(f"  Machine: Card Sorter")
    print(f"  Current settings:")
    print(f"    X feedrate: {X_FEEDRATE} mm/min ({X_FEEDRATE/60:.0f} mm/s)")
    print(f"    Z feedrate: {Z_FEEDRATE} mm/min ({Z_FEEDRATE/60:.0f} mm/s)")
    print(f"    Z probe:    {Z_PROBE_FEEDRATE} mm/min ({Z_PROBE_FEEDRATE/60:.0f} mm/s)")
    print(f"    Vacuum delay:   {VACUUM_ON_DELAY_MS}ms")
    print(f"    Pressure delay: {PRESSURE_ON_MS}ms")

    if not is_connected():
        print("\n  Connecting to board...")
        connect_to_board()
        if not is_connected():
            print("  ERROR: Could not connect. Exiting.")
            return

    # Read firmware settings
    print_subheader("Firmware Settings (M503)")
    fw = read_firmware_settings()
    if fw:
        for key in sorted(fw.keys()):
            print(f"    {key}: {fw[key]}")

        # Highlight important limits
        if 'max_feedrate_X' in fw:
            fw_max_x = fw['max_feedrate_X'] * 60  # M503 reports mm/s, we use mm/min
            print(f"\n  Firmware max X speed: {fw['max_feedrate_X']:.0f} mm/s "
                  f"({fw_max_x:.0f} mm/min)")
            if X_FEEDRATE > fw_max_x:
                print(f"  WARNING: Current X_FEEDRATE ({X_FEEDRATE}) exceeds "
                      f"firmware max ({fw_max_x:.0f})!")
        if 'max_feedrate_Z' in fw:
            fw_max_z = fw['max_feedrate_Z'] * 60
            print(f"  Firmware max Z speed: {fw['max_feedrate_Z']:.0f} mm/s "
                  f"({fw_max_z:.0f} mm/min)")
            if Z_FEEDRATE > fw_max_z:
                print(f"  WARNING: Current Z_FEEDRATE ({Z_FEEDRATE}) exceeds "
                      f"firmware max ({fw_max_z:.0f})!")
        if 'travel_accel' in fw:
            print(f"  Travel acceleration: {fw['travel_accel']:.0f} mm/s^2")
    else:
        print("    (Could not parse M503 output)")

    if args.info:
        close_connection()
        return

    # ------------------------------------------------------------------
    # Home the machine
    # ------------------------------------------------------------------
    print_subheader("Homing")
    print("  Homing all axes...")
    home_all()
    print("  Homed OK")

    # ------------------------------------------------------------------
    # Run benchmarks
    # ------------------------------------------------------------------
    best_x = X_FEEDRATE
    best_z = Z_FEEDRATE

    if run_all or args.x:
        x_results, best_x = benchmark_x_axis()

    if run_all or args.z:
        z_results, best_z = benchmark_z_axis()

    if run_all or args.accel:
        benchmark_acceleration()

    if run_all or args.repeat:
        benchmark_repeatability()

    if run_all or args.cycle:
        print_header("SORT CYCLE TIMING")

        # Current speeds
        _, t_current = benchmark_sort_cycle(
            X_FEEDRATE, Z_FEEDRATE, "Current settings")

        # If we found faster speeds, test those too
        if best_x > X_FEEDRATE or best_z > Z_FEEDRATE:
            _, t_fast = benchmark_sort_cycle(
                best_x, best_z, "Max reliable speeds")

            print_subheader("Speed Comparison")
            improvement = t_current - t_fast
            pct = improvement / t_current * 100 if t_current > 0 else 0
            print(f"  Current:  {t_current:.2f}s/card  "
                  f"({60/t_current:.1f}/min, {3600/t_current:.0f}/hr)")
            print(f"  Optimized: {t_fast:.2f}s/card  "
                  f"({60/t_fast:.1f}/min, {3600/t_fast:.0f}/hr)")
            print(f"  Saved:     {improvement:.2f}s/card ({pct:.1f}% faster)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print_header("RECOMMENDATIONS")
    print(f"  Best reliable X feedrate: {best_x} mm/min ({best_x/60:.0f} mm/s)")
    print(f"  Best reliable Z feedrate: {best_z} mm/min ({best_z/60:.0f} mm/s)")
    print()
    if best_x != X_FEEDRATE or best_z != Z_FEEDRATE:
        print("  To apply these speeds, update gcode_control.py:")
        if best_x != X_FEEDRATE:
            print(f"    X_FEEDRATE = {best_x}  # was {X_FEEDRATE}")
        if best_z != Z_FEEDRATE:
            print(f"    Z_FEEDRATE = {best_z}  # was {Z_FEEDRATE}")
    else:
        print("  Current settings appear optimal. No changes recommended.")

    print()
    print("  Other potential time savings:")
    print(f"    - Vacuum delay: currently {VACUUM_ON_DELAY_MS}ms "
          f"(test lower values manually)")
    print(f"    - Pressure delay: currently {PRESSURE_ON_MS}ms "
          f"(test lower values manually)")
    print(f"    - Z clear height: currently {Z_CLEAR_HEIGHT}mm "
          f"(lower = faster Z travel)")
    print(f"    - Probe caching: already implemented (saves ~1s per cached probe)")

    # Park and disconnect
    print()
    print("  Parking machine...")
    _send_and_wait("G90")
    timed_move(f"G0 Z{Z_MAX} F{Z_FEEDRATE}")
    timed_move(f"G0 X0 F{X_FEEDRATE}")
    close_connection()
    print("  Done!")


if __name__ == "__main__":
    main()
