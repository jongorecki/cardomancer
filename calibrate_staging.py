# calibrate_staging.py
# ---------------------------------------------------------------------------
# Calibrate the staging platform position and camera detection position.
#
# The staging platform sits on top of the bins, so its surface height
# also defines the bin clearance height (Z_CLEAR_HEIGHT).
#
# Flow:
#   1. Home machine, pick a card from the source bin
#   2. Move to current staging X at safe Z height, card held by vacuum
#   3. Adjust X and Z to position directly over the staging platform
#   4. Press 'p' to probe down and find the exact surface height
#   5. ENTER to confirm — this sets staging X, staging Z, and Z_CLEAR_HEIGHT
#   6. Card is dropped on staging, then camera position is calibrated
#   7. Save all values to staging_calibration.json and gcode_control.py
# ---------------------------------------------------------------------------

import os
import sys
import cv2
import json
import time
import re

print("Loading files, please wait...")

from detection import detect_card_on_staging
from gcode_control import (
    connect_to_board,
    close_connection,
    is_connected,
    home_all,
    wait_for_completion,
    all_pumps_off,
    z_to_top,
    pick_from_position,
    _send_and_wait,
    _send_gcode,
    _get_current_z,
    ser,
    X_SOURCE_BIN,
    X_STAGING_POSITION,
    X_CAMERA_POSITION,
    Z_CAMERA_POSITION,
    X_FEEDRATE,
    Z_FEEDRATE,
    Z_PROBE_FEEDRATE,
    Z_CLEAR_HEIGHT,
    Z_MAX,
    PRESSURE_ON_MS,
    VACUUM_ON_DELAY_MS,
)

# Step sizes
Z_STEP_COARSE = 10.0
Z_STEP_FINE = 2.0
X_STEP_COARSE = 10.0
X_STEP_FINE = 2.0
Z_MIN_SAFE = 20.0

CALIBRATION_FILE = os.path.join(os.path.dirname(__file__), "staging_calibration.json")


def move_z(z_pos):
    """Move Z to absolute position."""
    z_pos = max(Z_MIN_SAFE, min(Z_MAX, z_pos))
    _send_and_wait("G90")
    _send_and_wait(f"G0 Z{z_pos} F{Z_FEEDRATE}")
    _send_and_wait("M400")
    return z_pos


def move_x(x_pos):
    """Move X to absolute position."""
    x_pos = max(0, x_pos)
    _send_and_wait("G90")
    _send_and_wait(f"G0 X{x_pos} F{X_FEEDRATE}")
    _send_and_wait("M400")
    return x_pos


def probe_z():
    """Probe downward to find surface, return Z position."""
    _send_and_wait("G91")
    _send_and_wait(f"G38.2 Z-999 F{Z_PROBE_FEEDRATE}")
    _send_and_wait("M400")
    _send_and_wait("G90")
    z = _get_current_z()
    print(f"  [probe_z] M114 returned Z={z}")
    return z


def save_calibration(staging_x, staging_z, camera_x, camera_z):
    """Save calibration to JSON file."""
    data = {
        "staging_x": staging_x,
        "staging_z": staging_z,
        "camera_x": camera_x,
        "camera_z": camera_z,
    }
    with open(CALIBRATION_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Saved to {CALIBRATION_FILE}")


def load_calibration():
    """Load calibration from JSON file, return dict or None."""
    if os.path.exists(CALIBRATION_FILE):
        with open(CALIBRATION_FILE, "r") as f:
            return json.load(f)
    return None


def update_gcode_control(staging_x, staging_z, camera_x, camera_z):
    """Update constants in gcode_control.py."""
    gcode_path = os.path.join(os.path.dirname(__file__), "gcode_control.py")
    with open(gcode_path, "r") as f:
        content = f.read()

    replacements = {
        r"(X_STAGING_POSITION\s*=\s*)[\d.]+": f"\\g<1>{staging_x:.1f}",
        r"(X_CAMERA_POSITION\s*=\s*)[\d.]+": f"\\g<1>{camera_x:.1f}",
        r"(Z_CAMERA_POSITION\s*=\s*)[\d.]+": f"\\g<1>{camera_z:.1f}",
        r"(Z_STAGING_SURFACE\s*=\s*)[\d.]+": f"\\g<1>{staging_z:.1f}",
    }

    for pattern, replacement in replacements.items():
        content = re.sub(pattern, replacement, content)

    with open(gcode_path, "w") as f:
        f.write(content)

    print(f"  Updated gcode_control.py:")
    print(f"    X_STAGING_POSITION = {staging_x:.1f}")
    print(f"    Z_STAGING_SURFACE  = {staging_z:.1f}  (staging platform height)")
    print(f"    Z_CLEAR_HEIGHT     = {staging_z:.1f} + {15.0:.1f} margin  (travel height)")
    print(f"    X_CAMERA_POSITION  = {camera_x:.1f}")
    print(f"    Z_CAMERA_POSITION  = {camera_z:.1f}")


def print_controls_phase1():
    print("Controls:")
    print("  LEFT/RIGHT    — fine adjust X (+/- 2mm)")
    print("  HOME/END      — coarse adjust X (+/- 10mm)")
    print("  UP/DOWN       — fine adjust Z (+/- 2mm)")
    print("  PAGE UP/DN    — coarse adjust Z (+/- 10mm)")
    print("  'p'           — probe down to find surface height")
    print("  ENTER         — confirm staging position")
    print("  ESC           — exit without saving")
    print()


def print_controls_phase2():
    print("Controls:")
    print("  UP/DOWN       — fine adjust Z (+/- 2mm)")
    print("  PAGE UP/DN    — coarse adjust Z (+/- 10mm)")
    print("  LEFT/RIGHT    — fine adjust X (+/- 2mm)")
    print("  HOME/END      — coarse adjust X (+/- 10mm)")
    print("  'd'           — test detection at current position")
    print("  ENTER         — confirm camera position and save all")
    print("  ESC           — exit without saving")
    print()


def main():
    print("\n=== Staging Platform Calibration ===")
    print("Calibrate staging position, surface height, and camera position.")
    print("The staging platform height also sets Z_CLEAR_HEIGHT (bin max).\n")

    # Load previous calibration as defaults
    prev = load_calibration()
    if prev:
        print(f"  Previous calibration:")
        print(f"    Staging: X={prev['staging_x']:.1f}, Z={prev['staging_z']:.1f}")
        print(f"    Camera:  X={prev['camera_x']:.1f}, Z={prev['camera_z']:.1f}")
        print()

    connect_to_board()
    if not is_connected():
        print("ERROR: Could not connect to board.")
        return

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("ERROR: Could not open camera.")
        close_connection()
        return

    try:
        # ---- HOME AND PICK A CARD ----
        print("Homing all axes...")
        home_all()

        print("Picking card from source bin...")
        pick_from_position(X_SOURCE_BIN)
        print("  Card picked up. Moving to staging area.\n")

        # ---- PHASE 1: Position over staging platform (X + Z) ----
        print("--- PHASE 1: Staging Platform Position ---")
        print("Move X to center over the staging platform.")
        print("The card is held by vacuum so you can see alignment.")
        print("Press 'p' to probe down and find the surface height.")
        print()

        current_x = X_STAGING_POSITION
        current_z = Z_MAX  # Start at top (just homed)
        move_x(current_x)

        print(f"Starting at X={current_x:.1f}, Z={current_z:.1f}")
        print()
        print_controls_phase1()

        staging_z = None  # Will be set by probe

        while True:
            ret, frame = cap.read()
            if not ret:
                continue
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

            disp = frame.copy()
            cv2.putText(disp, "PHASE 1: Staging Position (holding card)", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
            z_text = f"X = {current_x:.1f}mm   Z = {current_z:.1f}mm"
            if staging_z is not None:
                z_text += f"   Surface = {staging_z:.1f}mm"
            cv2.putText(disp, z_text, (10, 65),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            cv2.putText(disp, "Arrows=move  p=probe  ENTER=confirm  ESC=exit",
                        (10, 95), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)
            cv2.imshow("Staging Calibration", disp)

            key = cv2.waitKeyEx(30)

            if key == 27:
                print("Exited without saving.")
                return

            elif key == 2424832:  # LEFT
                current_x = move_x(current_x - X_STEP_FINE)
                print(f"  X = {current_x:.1f}  (left {X_STEP_FINE}mm)")

            elif key == 2555904:  # RIGHT
                current_x = move_x(current_x + X_STEP_FINE)
                print(f"  X = {current_x:.1f}  (right {X_STEP_FINE}mm)")

            elif key == 2359296:  # HOME
                current_x = move_x(current_x - X_STEP_COARSE)
                print(f"  X = {current_x:.1f}  (left {X_STEP_COARSE}mm)")

            elif key == 2293760:  # END
                current_x = move_x(current_x + X_STEP_COARSE)
                print(f"  X = {current_x:.1f}  (right {X_STEP_COARSE}mm)")

            elif key == 2490368:  # UP
                current_z = move_z(current_z + Z_STEP_FINE)
                print(f"  Z = {current_z:.1f}  (up {Z_STEP_FINE}mm)")

            elif key == 2621440:  # DOWN
                current_z = move_z(current_z - Z_STEP_FINE)
                print(f"  Z = {current_z:.1f}  (down {Z_STEP_FINE}mm)")

            elif key == 2162688:  # PAGE UP
                current_z = move_z(current_z + Z_STEP_COARSE)
                print(f"  Z = {current_z:.1f}  (up {Z_STEP_COARSE}mm)")

            elif key == 2228224:  # PAGE DOWN
                current_z = move_z(current_z - Z_STEP_COARSE)
                print(f"  Z = {current_z:.1f}  (down {Z_STEP_COARSE}mm)")

            elif key == ord('p'):
                print("  Probing down to find surface...")
                z_found = probe_z()
                if z_found is not None:
                    staging_z = z_found
                    print(f"  Surface found at Z = {staging_z:.1f}mm")
                    # Lift back up above surface so card doesn't drag
                    current_z = move_z(staging_z + 20)
                    print(f"  Lifted to Z = {current_z:.1f}mm")
                else:
                    print("  WARNING: Probe did not return a Z value.")

            elif key == 13:  # ENTER
                if staging_z is None:
                    print("  You must probe first! Press 'p' to probe.")
                    continue
                staging_x = current_x
                print(f"\n  Staging position confirmed:")
                print(f"    X = {staging_x:.1f}mm")
                print(f"    Z_surface = {staging_z:.1f}mm (also sets Z_CLEAR_HEIGHT)")
                break

        # ---- DROP CARD ON STAGING ----
        print("\n  Dropping card on staging platform...")
        move_z(staging_z)
        # Release card
        _send_and_wait("M106 P0 S0")       # Vacuum off
        _send_and_wait("M106 P1 S255")      # Pressure on
        _send_and_wait(f"G4 P{PRESSURE_ON_MS}")
        _send_and_wait("M106 P1 S0")        # Pressure off
        # Lift back up
        current_z = move_z(staging_z + 30)
        print("  Card dropped.\n")

        # ---- PHASE 2: Camera detection position ----
        print("--- PHASE 2: Camera Detection Position ---")
        print("Adjust position until the camera can see the card clearly.")
        print("Press 'd' to test detection.\n")

        current_x = X_CAMERA_POSITION
        current_z = Z_CAMERA_POSITION
        move_z(current_z)
        move_x(current_x)

        print(f"Starting camera position at X={current_x:.1f}, Z={current_z:.1f}")
        print()
        print_controls_phase2()

        last_detection = None

        while True:
            ret, frame = cap.read()
            if not ret:
                continue
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

            disp = frame.copy()
            cv2.putText(disp, "PHASE 2: Camera Position", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            cv2.putText(disp, f"X = {current_x:.1f}mm   Z = {current_z:.1f}mm", (10, 65),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.putText(disp, "Arrows/PgUp/Dn/Home/End=adjust  d=detect  ENTER=save",
                        (10, 95), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)

            if last_detection is not None:
                status = "DETECTED" if last_detection else "NOT FOUND"
                color = (0, 255, 0) if last_detection else (0, 0, 255)
                cv2.putText(disp, f"Detection: {status}", (10, 125),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

            cv2.imshow("Staging Calibration", disp)

            key = cv2.waitKeyEx(30)

            if key == 27:
                print("Exited without saving.")
                return

            elif key == 2490368:  # UP
                current_z = move_z(current_z + Z_STEP_FINE)
                print(f"  Z = {current_z:.1f}")
                last_detection = None

            elif key == 2621440:  # DOWN
                current_z = move_z(current_z - Z_STEP_FINE)
                print(f"  Z = {current_z:.1f}")
                last_detection = None

            elif key == 2162688:  # PAGE UP
                current_z = move_z(current_z + Z_STEP_COARSE)
                print(f"  Z = {current_z:.1f}")
                last_detection = None

            elif key == 2228224:  # PAGE DOWN
                current_z = move_z(current_z - Z_STEP_COARSE)
                print(f"  Z = {current_z:.1f}")
                last_detection = None

            elif key == 2424832:  # LEFT
                current_x = move_x(current_x - X_STEP_FINE)
                print(f"  X = {current_x:.1f}")
                last_detection = None

            elif key == 2555904:  # RIGHT
                current_x = move_x(current_x + X_STEP_FINE)
                print(f"  X = {current_x:.1f}")
                last_detection = None

            elif key == 2359296:  # HOME
                current_x = move_x(current_x - X_STEP_COARSE)
                print(f"  X = {current_x:.1f}")
                last_detection = None

            elif key == 2293760:  # END
                current_x = move_x(current_x + X_STEP_COARSE)
                print(f"  X = {current_x:.1f}")
                last_detection = None

            elif key == ord('d'):
                print(f"  Testing detection...")
                for _ in range(15):
                    ret, frame = cap.read()
                    cv2.waitKey(1)
                ret, frame = cap.read()
                if ret:
                    frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
                    card_img = detect_card_on_staging(frame, debug=True)
                    if card_img is not None:
                        cv2.imshow("Detected Card", card_img)
                        print(f"  DETECTED")
                        last_detection = True
                    else:
                        print(f"  NOT FOUND")
                        last_detection = False

            elif key == 13:  # ENTER
                camera_x = current_x
                camera_z = current_z
                print(f"\n  Camera position confirmed: X={camera_x:.1f}, Z={camera_z:.1f}")
                break

        # ---- SAVE ----
        print("\n" + "=" * 60)
        print("  CALIBRATION COMPLETE")
        print("=" * 60)
        print(f"  Staging platform:  X = {staging_x:.1f}mm")
        print(f"  Staging surface:   Z = {staging_z:.1f}mm  (also Z_CLEAR_HEIGHT)")
        print(f"  Camera position:   X = {camera_x:.1f}mm, Z = {camera_z:.1f}mm")
        print()

        save_calibration(staging_x, staging_z, camera_x, camera_z)
        update_gcode_control(staging_x, staging_z, camera_x, camera_z)

        print()
        print("  The staging Z height is now used as Z_CLEAR_HEIGHT since the")
        print("  platform sits on top of the bins. Scripts will load the calibrated")
        print("  height at startup to skip probing the staging area.")

    except KeyboardInterrupt:
        print("\n[interrupted]")
    finally:
        all_pumps_off()
        z_to_top()
        wait_for_completion()
        cap.release()
        cv2.destroyAllWindows()
        close_connection()
        print("Done.")


if __name__ == "__main__":
    main()
