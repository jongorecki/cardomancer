#!/usr/bin/env python3
"""
Deeper diagnostic: for each no-detect frame, find the largest candidate
contour (passing area + ROI filters only) and compute multiple shape
metrics so we can see which ones separate fragmented-real-cards from
empty-platform noise.

Metrics computed per candidate:
  area                          - raw contour area
  solidity                      - contour_area / hull_area (current filter)
  rectangularity (contour)      - contour_area / bbox_area (current filter)
  hull_rectangularity           - hull_area / bbox_area       (NEW)
  polygon_4corners              - does approxPolyDP yield 4 corners?
  poly_area_over_min            - poly_area / min_area (if 4 corners)
"""

import os
import sys
import glob

import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import card_detect

NO_DETECT_GLOB = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_*", "no_detect", "nodet_*.jpg")

# Frames I've confirmed by-eye have a real card:
REAL_CARD_FRAMES = {
    "scan_logs/session_20260421_134408/no_detect/nodet_0014_attempt1.jpg",
    "scan_logs/session_20260421_140152/no_detect/nodet_0008_attempt1.jpg",
    "scan_logs/session_20260421_140152/no_detect/nodet_0010_attempt1.jpg",
    "scan_logs/session_20260421_115519/no_detect/nodet_0121_attempt1.jpg",
}


def largest_candidate(frame, min_area, max_area):
    """Find the single largest contour passing area + ROI only."""
    b, g, r = cv2.split(frame)
    edges = cv2.bitwise_or(
        cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), 50, 150),
        cv2.bitwise_or(
            cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 50, 150),
            cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), 50, 150),
        ),
    )

    k_rect = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    k_ellipse = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    closed = cv2.bitwise_or(
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_rect, iterations=3),
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_ellipse, iterations=3),
    )

    contours, _ = cv2.findContours(
        closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    best_area = 0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue

        M = cv2.moments(cnt)
        if M["m00"] > 0:
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]
            if not card_detect._is_inside_roi(cx, cy, margin=100):
                continue

        # Pick largest that passes area+ROI
        if area > best_area:
            best = cnt
            best_area = area
    return best, min_area


def compute_metrics(cnt, min_area):
    area = cv2.contourArea(cnt)
    hull = cv2.convexHull(cnt)
    hull_area = cv2.contourArea(hull)
    solidity = area / hull_area if hull_area > 0 else 0

    rect = cv2.minAreaRect(cnt)
    rw, rh = rect[1]
    box_area = rw * rh
    ratio = max(rw, rh) / max(min(rw, rh), 1)

    rectangularity = area / box_area if box_area > 0 else 0
    hull_rectangularity = hull_area / box_area if box_area > 0 else 0

    # Polygon check across multiple epsilons
    peri = cv2.arcLength(cnt, True)
    poly_4 = False
    poly_area_frac = 0.0
    poly_eps = None
    for eps_pct in [0.02, 0.04, 0.06, 0.08]:
        poly = cv2.approxPolyDP(cnt, eps_pct * peri, True)
        if len(poly) == 4:
            poly_4 = True
            poly_area_frac = cv2.contourArea(poly) / max(min_area, 1)
            poly_eps = eps_pct
            break

    # Try polygon on the HULL — sometimes raw contour is too noisy but hull simplifies to 4 corners
    hull_poly_4 = False
    hull_poly_area_frac = 0.0
    hull_peri = cv2.arcLength(hull, True)
    for eps_pct in [0.02, 0.04, 0.06, 0.08]:
        hpoly = cv2.approxPolyDP(hull, eps_pct * hull_peri, True)
        if len(hpoly) == 4:
            hull_poly_4 = True
            hull_poly_area_frac = cv2.contourArea(hpoly) / max(min_area, 1)
            break

    return {
        'area': int(area),
        'hull_area': int(hull_area),
        'ratio': ratio,
        'solidity': solidity,
        'rectangularity': rectangularity,
        'hull_rect': hull_rectangularity,
        'poly_4': poly_4,
        'poly_eps': poly_eps,
        'poly_af': poly_area_frac,
        'hull_poly_4': hull_poly_4,
        'hull_poly_af': hull_poly_area_frac,
    }


def main():
    # Dynamic area thresholds same as card_detect
    if card_detect._staging_roi_area and card_detect._staging_roi_area > 0:
        min_area = int(card_detect._staging_roi_area * 0.28)
        max_area = int(card_detect._staging_roi_area * 0.60)
    else:
        min_area = 80000
        max_area = 600000

    frames = sorted(glob.glob(NO_DETECT_GLOB))
    print(f"Found {len(frames)} no-detect frames. "
          f"min_area={min_area}, max_area={max_area}")
    print()

    header = (f"{'label':4s} {'frame':55s} "
              f"{'area':>8s} {'hull':>8s} {'ratio':>6s} "
              f"{'sol':>5s} {'rect':>5s} {'hrect':>5s} "
              f"{'p4':>2s} {'peps':>5s} {'paf':>5s} "
              f"{'hp4':>3s} {'hpaf':>5s}")
    print(header)
    print("-" * len(header))

    for path in frames:
        rel = os.path.relpath(path, SCRIPT_DIR).replace("\\", "/")
        label = "REAL" if rel in REAL_CARD_FRAMES else "empty"

        frame = cv2.imread(path)
        cnt, _ = largest_candidate(frame, min_area, max_area)
        if cnt is None:
            print(f"{label:4s} {rel:55s}   (no candidate passes area+ROI)")
            continue

        m = compute_metrics(cnt, min_area)
        eps_str = f"{m['poly_eps']:.2f}" if m['poly_eps'] else ""
        print(f"{label:4s} {rel:55s} "
              f"{m['area']:>8d} {m['hull_area']:>8d} {m['ratio']:>6.2f} "
              f"{m['solidity']:>5.2f} {m['rectangularity']:>5.2f} "
              f"{m['hull_rect']:>5.2f} "
              f"{'Y' if m['poly_4'] else 'n':>2s} "
              f"{eps_str:>5s} "
              f"{m['poly_af']:>5.2f} "
              f"{'Y' if m['hull_poly_4'] else 'n':>3s} "
              f"{m['hull_poly_af']:>5.2f}")


if __name__ == "__main__":
    main()
