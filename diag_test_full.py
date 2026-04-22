#!/usr/bin/env python3
"""
Sweep Canny + morph combinations to find any setting that detects all 5
real-card failures while still rejecting the ~19 empty frames.

Checks for:
- Real card detected: any contour with area 250k-800k, ratio 1.0-2.0,
  solidity>=0.60, rect>=0.60, polygon simplifies to 4 corners.
- Empty rejected: no such contour.
"""

import os
import sys
import glob
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import card_detect

REAL_CARD_FRAMES = {
    "scan_logs/session_20260421_134408/no_detect/nodet_0014_attempt1.jpg",
    "scan_logs/session_20260421_140152/no_detect/nodet_0008_attempt1.jpg",
    "scan_logs/session_20260421_140152/no_detect/nodet_0010_attempt1.jpg",
    "scan_logs/session_20260421_115519/no_detect/nodet_0121_attempt1.jpg",
    "scan_logs/session_20260421_140152/no_detect/nodet_0076_attempt1.jpg",
}

NO_DETECT_GLOB = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_*", "no_detect", "nodet_*.jpg")


def detect(frame, canny_low, canny_high, kernel_size, morph_iter,
           min_area, max_area,
           sol_thresh=0.65, rect_thresh=0.70, ratio_tol=0.35):
    b, g, r = cv2.split(frame)
    edges = cv2.bitwise_or(
        cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), canny_low, canny_high),
        cv2.bitwise_or(
            cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), canny_low, canny_high),
            cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), canny_low, canny_high),
        ),
    )
    k_rect = cv2.getStructuringElement(
        cv2.MORPH_RECT, (kernel_size, kernel_size))
    k_ellipse = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    closed = cv2.bitwise_or(
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_rect, iterations=morph_iter),
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_ellipse, iterations=morph_iter),
    )
    contours, _ = cv2.findContours(
        closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area or area > max_area:
            continue
        M = cv2.moments(cnt)
        if M["m00"] > 0:
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]
            if not card_detect._is_inside_roi(cx, cy, margin=100):
                continue

        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        solidity = area / hull_area if hull_area > 0 else 0
        rect = cv2.minAreaRect(cnt)
        rw, rh = rect[1]
        if min(rw, rh) < 1:
            continue
        ratio = max(rw, rh) / min(rw, rh)
        box_area = rw * rh
        rectangularity = area / box_area if box_area > 0 else 0

        if abs(ratio - 1.396) / 1.396 > ratio_tol:
            continue
        if solidity < sol_thresh:
            continue
        if rectangularity < rect_thresh:
            continue

        # 4-corner polygon
        peri = cv2.arcLength(cnt, True)
        for eps in (0.02, 0.04, 0.06, 0.08):
            p = cv2.approxPolyDP(cnt, eps * peri, True)
            if len(p) == 4:
                return True
    return False


def main():
    if card_detect._staging_roi_area and card_detect._staging_roi_area > 0:
        min_area = int(card_detect._staging_roi_area * 0.28)
        max_area = int(card_detect._staging_roi_area * 0.60)
    else:
        min_area, max_area = 80000, 600000

    frames = sorted(glob.glob(NO_DETECT_GLOB))
    real_paths = []
    empty_paths = []
    for p in frames:
        rel = os.path.relpath(p, SCRIPT_DIR).replace("\\", "/")
        if rel in REAL_CARD_FRAMES:
            real_paths.append(p)
        else:
            empty_paths.append(p)
    real_frames = [(p, cv2.imread(p)) for p in real_paths]
    empty_frames = [(p, cv2.imread(p)) for p in empty_paths]
    print(f"Real: {len(real_frames)}  Empty: {len(empty_frames)}")
    print(f"min_area={min_area}, max_area={max_area}")
    print()

    # Grid search: Canny thresholds x morph kernel x morph iterations
    configs = []
    for lo, hi in [(30, 90), (40, 120), (50, 150), (60, 180), (80, 200)]:
        for ks in (3, 5, 7):
            for mi in (1, 2, 3):
                configs.append((lo, hi, ks, mi))

    # Build results
    print(f"{'canny':>8s} {'k':>2s} {'iter':>4s}  {'real':>5s} {'empty-FP':>9s}")
    print("-" * 40)

    best_real = 0
    best_configs = []
    for lo, hi, ks, mi in configs:
        real_hits = sum(1 for _, f in real_frames
                        if detect(f, lo, hi, ks, mi, min_area, max_area))
        empty_fps = sum(1 for _, f in empty_frames
                        if detect(f, lo, hi, ks, mi, min_area, max_area))
        if real_hits >= best_real:
            if real_hits > best_real:
                best_real = real_hits
                best_configs = []
            best_configs.append((lo, hi, ks, mi, empty_fps))
        marker = " *" if real_hits == 5 else (" +" if real_hits >= 3 else "")
        print(f"{lo:>3d}/{hi:<3d}  {ks:>2d} {mi:>4d}  {real_hits:>5d} {empty_fps:>9d}{marker}")

    print()
    print(f"Best: {best_real}/{len(real_frames)} real cards detected by:")
    for lo, hi, ks, mi, fp in best_configs:
        print(f"  canny={lo}/{hi} kernel={ks}x{ks} iter={mi}  ({fp} empty FPs)")


if __name__ == "__main__":
    main()
