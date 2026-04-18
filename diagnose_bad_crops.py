#!/usr/bin/env python3
# diagnose_bad_crops.py
# Re-run detect_card() on the misidentified scans and save the result
# alongside the original camera frame (with detected contour drawn)
# for visual inspection.

import os
import sys
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from card_detect import (
    detect_card,
    _find_card_contour,
    _perspective_warp,
    CARD_WIDTH,
    CARD_HEIGHT,
)

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
FRAMES_DIR = os.path.join(SESSION_DIR, "scan_images")
OUT_DIR = os.path.join(SCRIPT_DIR, "diag_bad_crops")
os.makedirs(OUT_DIR, exist_ok=True)

# The 12 misidentifications from the regression run
BAD_SCANS = [
    (27, "Invasion of Azgol"),
    (96, "How to Keep an Izzet Mage Busy"),
    (118, "Increasing Vengeance"),
    (140, "Silumgar's Scorn"),
    (143, "Stalking Bloodsucker"),
    (193, "Norn's Inquisitor"),
    (205, "Zombie Infestation"),
    (215, "Mountain"),
    (216, "Swamp"),
    (241, "Ripscale Predator"),
    (271, "Flood of Recollection"),
    (356, "Lunarch Mantle"),
]


def main():
    for scan_num, expected in BAD_SCANS:
        frame_path = os.path.join(FRAMES_DIR, f"scan_{scan_num:04d}.jpg")
        if not os.path.exists(frame_path):
            print(f"  skip {scan_num}: no frame")
            continue

        frame = cv2.imread(frame_path)
        if frame is None:
            continue

        # Run detection to get the contour
        contour = _find_card_contour(frame)
        if contour is None:
            print(f"  scan {scan_num}: NO CONTOUR  ({expected})")
            # Still save the frame so we can see why
            out = os.path.join(OUT_DIR, f"bad_{scan_num:04d}_noctr.jpg")
            cv2.imwrite(out, frame)
            continue

        # Draw contour on original frame (scaled down)
        annotated = frame.copy()
        pts = contour.reshape(-1, 2).astype(np.int32)
        cv2.polylines(annotated, [pts], True, (0, 255, 0), 4)
        for p in pts:
            cv2.circle(annotated, tuple(p), 12, (0, 0, 255), -1)

        # Generate warped crop
        warped = _perspective_warp(frame, contour)

        # Compose side-by-side: annotated frame (left) + warped crop (right)
        # Scale annotated to 520 tall
        scale = 520 / annotated.shape[0]
        ann_small = cv2.resize(
            annotated, (int(annotated.shape[1] * scale), 520))
        # Warped is already 745x1040 -> scale to 370x520
        warped_small = cv2.resize(warped, (370, 520))

        combined = np.hstack([ann_small, warped_small])

        # Label with scan number + expected name
        label = f"scan {scan_num}: {expected}"
        cv2.putText(combined, label, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        out = os.path.join(OUT_DIR, f"bad_{scan_num:04d}.jpg")
        cv2.imwrite(out, combined)
        print(f"  scan {scan_num}: saved  ({expected})")

    print(f"\nOutput: {OUT_DIR}")


if __name__ == '__main__':
    main()
