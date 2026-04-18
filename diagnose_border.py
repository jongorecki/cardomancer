#!/usr/bin/env python3
# diagnose_border.py
# Zoomed-in visualization of the detected contour to determine whether
# Canny is locking onto the inner colored frame or the outer card edge.

import os
import sys
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from card_detect import _find_card_contour

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
FRAMES_DIR = os.path.join(SESSION_DIR, "scan_images")
OUT_DIR = os.path.join(SCRIPT_DIR, "diag_border")
os.makedirs(OUT_DIR, exist_ok=True)

# The 6 scans that weren't already corrected and are still "wrong"
# (i.e. ones where the hash disagreed with the sort-time result).
# Plus one known-good for control.
SCANS = [27, 140, 143, 193, 241, 271, 100]


def main():
    for scan_num in SCANS:
        frame_path = os.path.join(FRAMES_DIR, f"scan_{scan_num:04d}.jpg")
        if not os.path.exists(frame_path):
            continue

        frame = cv2.imread(frame_path)
        if frame is None:
            continue

        contour = _find_card_contour(frame)
        if contour is None:
            print(f"  scan {scan_num:4d}: no contour")
            out = os.path.join(OUT_DIR, f"border_{scan_num:04d}_nodet.jpg")
            cv2.imwrite(out, frame)
            continue

        pts = contour.reshape(-1, 2).astype(np.int32)

        # Compute tight bbox + 60px margin
        x, y, w, h = cv2.boundingRect(pts)
        pad = 60
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(frame.shape[1], x + w + pad)
        y1 = min(frame.shape[0], y + h + pad)
        crop = frame[y0:y1, x0:x1].copy()

        # Draw the detected polygon on the zoomed frame (adjust coords)
        shifted = pts.copy()
        shifted[:, 0] -= x0
        shifted[:, 1] -= y0
        cv2.polylines(crop, [shifted], True, (0, 255, 0), 2)
        for p in shifted:
            cv2.circle(crop, tuple(p), 6, (0, 0, 255), -1)

        # Scale up 2x so we can really see the edge
        zoom = cv2.resize(crop, None, fx=2.0, fy=2.0,
                         interpolation=cv2.INTER_CUBIC)

        cv2.putText(zoom, f"scan {scan_num}", (12, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)

        out = os.path.join(OUT_DIR, f"border_{scan_num:04d}.jpg")
        cv2.imwrite(out, zoom)
        print(f"  scan {scan_num:4d}: saved zoom {zoom.shape[1]}x{zoom.shape[0]}")

    print(f"\nOutput: {OUT_DIR}")


if __name__ == '__main__':
    main()
