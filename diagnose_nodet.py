#!/usr/bin/env python3
# diagnose_nodet.py
# Find out WHY detection is failing on the new no-detect scans.

import os
import sys
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import card_detect as cd

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
FRAMES_DIR = os.path.join(SESSION_DIR, "scan_images")
OUT_DIR = os.path.join(SCRIPT_DIR, "diag_nodet")
os.makedirs(OUT_DIR, exist_ok=True)

# A mix of new no-detects
SCANS = [92, 178, 180, 211, 224, 291, 295, 312, 334]


def main():
    # Load staging ROI directly
    roi_area = cd._staging_roi_area or 868474
    min_area = int(roi_area * 0.28)
    max_area = int(roi_area * 0.60)
    print(f"min_area={min_area} max_area={max_area}")
    print()

    for scan_num in SCANS:
        frame_path = os.path.join(FRAMES_DIR, f"scan_{scan_num:04d}.jpg")
        frame = cv2.imread(frame_path)
        if frame is None:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        b, g, r = cv2.split(frame)
        edges_mc = cv2.bitwise_or(
            cv2.Canny(cv2.GaussianBlur(b, (3,3), 0), 50, 150),
            cv2.bitwise_or(
                cv2.Canny(cv2.GaussianBlur(g, (3,3), 0), 50, 150),
                cv2.Canny(cv2.GaussianBlur(r, (3,3), 0), 50, 150),
            ),
        )

        k9 = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        closed_9 = cv2.morphologyEx(edges_mc, cv2.MORPH_CLOSE, k9, iterations=3)

        contours, _ = cv2.findContours(
            closed_9, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Report the top-3 contours by area
        by_area = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
        print(f"scan {scan_num}:")
        for i, c in enumerate(by_area):
            area = cv2.contourArea(c)
            x, y, w, h = cv2.boundingRect(c)
            rect = cv2.minAreaRect(c)
            rw, rh = rect[1]
            if min(rw, rh) < 1:
                ratio = 0
            else:
                ratio = max(rw, rh) / min(rw, rh)
            hull = cv2.convexHull(c)
            hull_area = cv2.contourArea(hull) or 1
            sol = area / hull_area
            rect_fill = area / (rw * rh) if rw > 0 and rh > 0 else 0

            in_range = min_area <= area <= max_area
            status = "OK" if in_range else ("TOO SMALL" if area < min_area else "TOO BIG")
            print(f"  #{i+1}: area={area:7.0f} {status:9s}  "
                  f"bbox {w:4d}x{h:4d}  ratio={ratio:.2f}  "
                  f"solidity={sol:.2f}  rect_fill={rect_fill:.2f}")

        # Also render the frame with the largest contour outlined in red
        vis = frame.copy()
        if by_area:
            cv2.drawContours(vis, [by_area[0]], -1, (0, 0, 255), 3)
        cv2.imwrite(os.path.join(OUT_DIR, f"nodet_{scan_num:04d}.jpg"), vis)

        print()


if __name__ == '__main__':
    main()
