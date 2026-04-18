#!/usr/bin/env python
"""
test_border_detection.py
========================
Visual test of card boundary detection on the staging platform.

Usage:
  python test_border_detection.py                     # live camera
  python test_border_detection.py scan_logs/session_20260412_154026  # replay session

In replay mode, loads scan images from a session directory and runs
detect_card_on_staging() on each, showing:
  - Left: original frame with detected contour drawn in green
  - Right: the warped 745x1040 card image (or "NO CARD" placeholder)

Controls:
  SPACE / RIGHT ARROW  — next image
  LEFT ARROW           — previous image
  D                    — toggle debug mode (shows intermediate steps)
  Q / ESC              — quit

In live mode, grabs frames from the camera continuously and shows
the detected contour + warp in real-time.
"""

import sys
import os
import glob
import cv2
import numpy as np

# Add scripts dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from detection import (detect_card_on_staging,
                        _find_card_contour_color_seg,
                        _find_card_contour_heavy_blur,
                        find_card_contour_no_bg,
                        get_perspective_corrected_card,
                        load_staging_roi)
from config import CARD_WIDTH, CARD_HEIGHT


def find_contour_for_display(frame, roi=None, debug=False):
    """Find card contour using the same strategy as detect_card_on_staging."""
    # Strategy 1: Color segmentation (best, needs ROI)
    if roi is not None:
        contour = _find_card_contour_color_seg(
            frame, roi, min_area=50000, expected_ratio=1.4,
            ratio_tolerance=0.25, debug=debug)
        if contour is not None:
            return contour, "color_seg"

    # Strategy 2: Heavy blur (outer border)
    contour = _find_card_contour_heavy_blur(
        frame, min_area=10000, expected_ratio=1.4,
        ratio_tolerance=0.25, debug=debug)
    if contour is not None:
        return contour, "heavy_blur"

    # Strategy 3: Standard Canny
    contour = find_card_contour_no_bg(
        frame, min_area=10000, expected_ratio=1.4,
        ratio_tolerance=0.25, debug=debug)
    if contour is not None:
        return contour, "standard_canny"

    return None, None


def draw_contour_on_frame(frame, contour, method=None):
    """Draw the detected card contour on a copy of the frame."""
    vis = frame.copy()
    if contour is not None:
        cv2.drawContours(vis, [contour], -1, (0, 255, 0), 3)
        for pt in contour.reshape(-1, 2):
            cv2.circle(vis, (int(pt[0]), int(pt[1])), 8, (0, 0, 255), -1)
        area = cv2.contourArea(contour)
        x, y, w, h = cv2.boundingRect(contour)
        ratio = max(w, h) / (min(w, h) + 1e-6)
        label = f"Area:{area:.0f} Ratio:{ratio:.2f}"
        if method:
            label += f" [{method}]"
        cv2.putText(vis, label, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    else:
        cv2.putText(vis, "NO CONTOUR FOUND", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    return vis


def replay_session(session_dir):
    """Replay saved scan images from a session directory."""
    scan_dir = os.path.join(session_dir, "scan_images")
    if not os.path.isdir(scan_dir):
        if os.path.isdir(session_dir) and glob.glob(os.path.join(session_dir, "scan_*.jpg")):
            scan_dir = session_dir
        else:
            print(f"No scan_images directory found in {session_dir}")
            return

    files = sorted(glob.glob(os.path.join(scan_dir, "scan_*.jpg")))
    if not files:
        files = sorted(glob.glob(os.path.join(scan_dir, "*.jpg")))
    if not files:
        print(f"No images found in {scan_dir}")
        return

    # Load staging ROI for color segmentation
    roi = load_staging_roi()
    if roi is not None:
        print(f"Loaded staging ROI — color segmentation enabled")
    else:
        print(f"No staging ROI — falling back to edge-based detection")

    print(f"Found {len(files)} scan images in {scan_dir}")
    print("Controls: SPACE/RIGHT=next, LEFT=prev, D=debug, Q/ESC=quit")

    idx = 0
    debug_mode = False
    window_name = "Border Detection Test"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    while True:
        frame = cv2.imread(files[idx])
        if frame is None:
            print(f"Failed to load {files[idx]}")
            idx = (idx + 1) % len(files)
            continue

        # Detect contour
        contour, method = find_contour_for_display(frame, roi=roi, debug=debug_mode)

        # Draw contour on frame
        vis_frame = draw_contour_on_frame(frame, contour, method)

        # Warp if contour found
        if contour is not None:
            card_img = get_perspective_corrected_card(frame, contour,
                                                      width=CARD_WIDTH,
                                                      height=CARD_HEIGHT)
        else:
            card_img = np.zeros((CARD_HEIGHT, CARD_WIDTH, 3), dtype=np.uint8)
            cv2.putText(card_img, "NO CARD", (200, 520),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 3)

        # Scale the frame to fit next to the card image
        fh, fw = vis_frame.shape[:2]
        scale = CARD_HEIGHT / fh
        vis_small = cv2.resize(vis_frame, (int(fw * scale), CARD_HEIGHT))

        # Side by side
        combined = np.hstack([vis_small, card_img])

        # Label
        fname = os.path.basename(files[idx])
        cv2.putText(combined, f"[{idx+1}/{len(files)}] {fname}  (D=debug)",
                    (10, combined.shape[0] - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

        cv2.imshow(window_name, combined)

        key = cv2.waitKey(0) & 0xFF
        if key in (ord('q'), 27):
            break
        elif key in (ord(' '), 83, ord('n')):
            idx = (idx + 1) % len(files)
        elif key == 81:
            idx = (idx - 1) % len(files)
        elif key == ord('d'):
            debug_mode = not debug_mode
            print(f"Debug mode: {'ON' if debug_mode else 'OFF'}")

    cv2.destroyAllWindows()


def live_camera():
    """Live camera mode — detect card borders in real-time."""
    print("Opening camera...")
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Failed to open camera")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

    # Load staging ROI for color segmentation
    roi = load_staging_roi()
    if roi is not None:
        print(f"Loaded staging ROI — color segmentation enabled")
    else:
        print(f"No staging ROI — falling back to edge-based detection")

    print("Controls: D=toggle debug, Q/ESC=quit")

    debug_mode = False
    window_name = "Border Detection Test (Live)"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        contour, method = find_contour_for_display(frame, roi=roi, debug=debug_mode)
        vis_frame = draw_contour_on_frame(frame, contour, method)

        # Show warped card in corner
        if contour is not None:
            card_img = get_perspective_corrected_card(frame, contour,
                                                      width=CARD_WIDTH,
                                                      height=CARD_HEIGHT)
            preview_h = frame.shape[0] // 3
            preview_w = int(preview_h * CARD_WIDTH / CARD_HEIGHT)
            preview = cv2.resize(card_img, (preview_w, preview_h))
            y_off = frame.shape[0] - preview_h
            x_off = frame.shape[1] - preview_w
            vis_frame[y_off:y_off+preview_h, x_off:x_off+preview_w] = preview

        cv2.imshow(window_name, vis_frame)

        key = cv2.waitKey(30) & 0xFF
        if key in (ord('q'), 27):
            break
        elif key == ord('d'):
            debug_mode = not debug_mode
            print(f"Debug mode: {'ON' if debug_mode else 'OFF'}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == '__main__':
    if len(sys.argv) > 1:
        replay_session(sys.argv[1])
    else:
        live_camera()
