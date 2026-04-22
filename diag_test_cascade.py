#!/usr/bin/env python3
"""
Test a simple 2-pass cascade: primary pass with tight morph (3x3 iter=1)
for high-contrast cards that would merge with background under strong
morph, fallback to current 5x5 iter=3 for low-contrast cards that need
gap-bridging.

Apply to ALL no-detect frames (both real and empty) to confirm:
  - Real cards get detected
  - Empty platforms still reject
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
}

NO_DETECT_GLOB = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_*", "no_detect", "nodet_*.jpg")


def run_pass(frame, kernel_size, morph_iter, min_area, max_area):
    """Return list of candidate contours with good shape characteristics."""
    b, g, r = cv2.split(frame)
    edges = cv2.bitwise_or(
        cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), 50, 150),
        cv2.bitwise_or(
            cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 50, 150),
            cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), 50, 150),
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

    results = []
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
        box_area = rw * rh
        ratio = max(rw, rh) / max(min(rw, rh), 1)
        rectangularity = area / box_area if box_area > 0 else 0

        # Shape filter
        ratio_ok = abs(ratio - 1.396) / 1.396 <= 0.35
        rect_ok = rectangularity >= 0.70
        sol_ok = solidity >= 0.65

        # Polygon check
        peri = cv2.arcLength(cnt, True)
        poly4 = None
        for eps in (0.02, 0.04, 0.06, 0.08):
            p = cv2.approxPolyDP(cnt, eps * peri, True)
            if len(p) == 4:
                poly4 = p
                break

        results.append({
            'cnt': cnt, 'area': area, 'ratio': ratio,
            'solidity': solidity, 'rectangularity': rectangularity,
            'poly4': poly4,
            'shape_ok': ratio_ok and rect_ok and sol_ok,
        })
    return results


def detect_2pass(frame):
    """Two-pass detection: tight morph first, loose morph fallback."""
    if card_detect._staging_roi_area and card_detect._staging_roi_area > 0:
        min_area = int(card_detect._staging_roi_area * 0.28)
        max_area = int(card_detect._staging_roi_area * 0.60)
    else:
        min_area, max_area = 80000, 600000

    # Pass 1: tight morph (3x3 iter=1) — card inner frame, high contrast
    p1 = run_pass(frame, 3, 1, min_area, max_area)
    accepted_p1 = [r for r in p1 if r['shape_ok']]
    if accepted_p1:
        accepted_p1.sort(key=lambda x: -x['area'])
        return 'pass1', accepted_p1[0]

    # Pass 2: current loose morph (5x5 iter=3) — bridges corner gaps
    p2 = run_pass(frame, 5, 3, min_area, max_area)
    accepted_p2 = [r for r in p2 if r['shape_ok']]
    if accepted_p2:
        accepted_p2.sort(key=lambda x: -x['area'])
        return 'pass2', accepted_p2[0]

    return None, None


def main():
    frames = sorted(glob.glob(NO_DETECT_GLOB))
    real_ok = 0
    real_fail = 0
    empty_ok = 0       # empty correctly rejected
    empty_wrong = 0    # empty mistakenly detected

    for path in frames:
        rel = os.path.relpath(path, SCRIPT_DIR).replace("\\", "/")
        frame = cv2.imread(path)
        is_real = rel in REAL_CARD_FRAMES

        which, result = detect_2pass(frame)
        if is_real:
            if result is not None:
                real_ok += 1
                print(f"  OK   REAL  {which}  {rel}")
                print(f"       area={int(result['area'])} ratio={result['ratio']:.2f} "
                      f"sol={result['solidity']:.2f} "
                      f"rect={result['rectangularity']:.2f}")
            else:
                real_fail += 1
                print(f"  FAIL REAL       {rel}")
        else:
            if result is not None:
                empty_wrong += 1
                print(f"  FP   empty {which} {rel}")
                print(f"       area={int(result['area'])} ratio={result['ratio']:.2f} "
                      f"sol={result['solidity']:.2f} "
                      f"rect={result['rectangularity']:.2f}")
            else:
                empty_ok += 1
                # silent

    print()
    print("=" * 72)
    print(f"Real cards detected: {real_ok}/{real_ok + real_fail}")
    print(f"Empty platforms still rejected: {empty_ok}/{empty_ok + empty_wrong}")


if __name__ == "__main__":
    main()
