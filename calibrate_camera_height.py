# calibrate_camera_height.py
# ---------------------------------------------------------------------------
# Find the minimum Z height above the staging area where the camera
# (mounted on the X carriage) can see the card clearly enough for detection.
#
# Flow:
#   1. Home, pick a card from source, drop on staging area
#   2. Lift Z incrementally, showing live camera feed at each height
#   3. At each height, press 'd' to test detection
#   4. Press UP/DOWN arrows to adjust Z in small steps
#   5. Press SPACE to save the optimal height
# ---------------------------------------------------------------------------

import os
import sys
import cv2
import numpy as np
import time

print("Loading files, please wait...")

from detection import detect_card_on_staging
from gcode_control import (
    connect_to_board,
    close_connection,
    is_connected,
    home_all,
    pick_from_position,
    drop_on_surface,
    wait_for_completion,
    all_pumps_off,
    z_to_top,
    _send_and_wait,
    X_SOURCE_BIN,
    X_STAGING_POSITION,
    X_FEEDRATE,
    Z_FEEDRATE,
)

# Starting height and step size
Z_START = 140.0       # Start fairly high above staging area
Z_STEP_COARSE = 10.0  # mm per coarse step (PAGE UP/DOWN)
Z_STEP_FINE = 2.0     # mm per fine step (UP/DOWN arrows)
X_STEP_COARSE = 10.0  # mm per coarse X step (SHIFT + LEFT/RIGHT)
X_STEP_FINE = 2.0     # mm per fine X step (LEFT/RIGHT)
Z_MIN_SAFE = 50.0     # Don't go lower than this (card contact area)
Z_MAX = 220.0


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


def main():
    print("\n=== Camera Height Calibration ===")
    print("Find the minimum Z height where camera can see the card.\n")

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
        # Home
        print("Homing all axes...")
        home_all()

        # Pick and stage a card
        print("Picking card from source...")
        pick_from_position(X_SOURCE_BIN)
        print("Dropping on staging area...")
        drop_on_surface(X_STAGING_POSITION)

        # Start at initial position
        current_z = move_z(Z_START)
        current_x = X_STAGING_POSITION
        print(f"\nStarting at X={current_x:.1f}, Z={current_z:.1f}")
        print()
        print("Controls:")
        print("  UP/DOWN       — fine adjust Z (+/- 2mm)")
        print("  PAGE UP/DN    — coarse adjust Z (+/- 10mm)")
        print("  LEFT/RIGHT    — fine adjust X (+/- 2mm)")
        print("  HOME/END      — coarse adjust X (+/- 10mm)")
        print("  'd'           — test detection at current position")
        print("  's'           — save this position and exit")
        print("  ESC           — exit without saving")
        print()

        last_detection = None

        while True:
            ret, frame = cap.read()
            if not ret:
                continue
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

            # Show current position on frame
            disp = frame.copy()
            cv2.putText(disp, f"X = {current_x:.1f}mm   Z = {current_z:.1f}mm", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.putText(disp, "Arrows=fine  PgUp/Dn,Home/End=coarse  d=detect  s=save",
                        (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)

            if last_detection is not None:
                status = "DETECTED" if last_detection else "NOT FOUND"
                color = (0, 255, 0) if last_detection else (0, 0, 255)
                cv2.putText(disp, f"Detection: {status}", (10, 90),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

            cv2.imshow("Camera Height Calibration", disp)

            key = cv2.waitKeyEx(30)

            if key == 27:  # ESC
                print("Exited without saving.")
                break

            elif key == 2490368:  # UP arrow
                current_z = move_z(current_z + Z_STEP_FINE)
                print(f"  Z = {current_z:.1f}  (up {Z_STEP_FINE}mm)")
                last_detection = None

            elif key == 2621440:  # DOWN arrow
                current_z = move_z(current_z - Z_STEP_FINE)
                print(f"  Z = {current_z:.1f}  (down {Z_STEP_FINE}mm)")
                last_detection = None

            elif key == 2162688:  # PAGE UP
                current_z = move_z(current_z + Z_STEP_COARSE)
                print(f"  Z = {current_z:.1f}  (up {Z_STEP_COARSE}mm)")
                last_detection = None

            elif key == 2228224:  # PAGE DOWN
                current_z = move_z(current_z - Z_STEP_COARSE)
                print(f"  Z = {current_z:.1f}  (down {Z_STEP_COARSE}mm)")
                last_detection = None

            elif key == 2424832:  # LEFT arrow
                current_x = move_x(current_x - X_STEP_FINE)
                print(f"  X = {current_x:.1f}  (left {X_STEP_FINE}mm)")
                last_detection = None

            elif key == 2555904:  # RIGHT arrow
                current_x = move_x(current_x + X_STEP_FINE)
                print(f"  X = {current_x:.1f}  (right {X_STEP_FINE}mm)")
                last_detection = None

            elif key == 2359296:  # HOME
                current_x = move_x(current_x - X_STEP_COARSE)
                print(f"  X = {current_x:.1f}  (left {X_STEP_COARSE}mm)")
                last_detection = None

            elif key == 2293760:  # END
                current_x = move_x(current_x + X_STEP_COARSE)
                print(f"  X = {current_x:.1f}  (right {X_STEP_COARSE}mm)")
                last_detection = None

            elif key == ord('d'):
                # Flush camera buffer then detect
                print(f"  Testing detection at Z={current_z:.1f}...")
                for _ in range(15):
                    ret, frame = cap.read()
                    cv2.waitKey(1)
                ret, frame = cap.read()
                if ret:
                    frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
                    card_img = detect_card_on_staging(frame, debug=True)
                    if card_img is not None:
                        cv2.imshow("Detected Card", card_img)
                        print(f"  DETECTED at Z={current_z:.1f}")
                        last_detection = True
                    else:
                        print(f"  NOT FOUND at Z={current_z:.1f}")
                        last_detection = False

            elif key == ord('s'):
                print(f"\n  === SAVED ===")
                print(f"  Camera position: X={current_x:.1f}mm, Z={current_z:.1f}mm")
                print(f"  X offset from staging: {current_x - X_STAGING_POSITION:.1f}mm")
                print(f"  Use these as Z_CAMERA_HEIGHT / X_CAMERA_OFFSET in gcode_control.py")
                break

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
