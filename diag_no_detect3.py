#!/usr/bin/env python3
"""
Per-channel diagnostic: for REAL card frames only, show the largest
candidate from each Canny channel (gray / B / G / R / multi-OR).

Question: is any single channel finding a clean card-sized contour
(hull ~850k, ratio ~1.40, hrect ~0.95) that the multi-OR approach merges
with surrounding clutter?
"""

import os
import sys

import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import card_detect

REAL_CARD_FRAMES = [
    "scan_logs/session_20260421_134408/no_detect/nodet_0014_attempt1.jpg",
    "scan_logs/session_20260421_140152/no_detect/nodet_0008_attempt1.jpg",
    "scan_logs/session_20260421_140152/no_detect/nodet_0010_attempt1.jpg",
    "scan_logs/session_20260421_115519/no_detect/nodet_0121_attempt1.jpg",
]


def process_channel(edges, min_area, max_area):
    """Return list of candidate contours with their metrics."""
    k_rect = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    k_ellipse = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    closed = cv2.bitwise_or(
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_rect, iterations=3),
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_ellipse, iterations=3),
    )
    contours, _ = cv2.findContours(
        closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    results = []
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

        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        solidity = area / hull_area if hull_area > 0 else 0

        rect = cv2.minAreaRect(cnt)
        rw, rh = rect[1]
        box_area = rw * rh
        ratio = max(rw, rh) / max(min(rw, rh), 1)
        rectangularity = area / box_area if box_area > 0 else 0
        hull_rect = hull_area / box_area if box_area > 0 else 0

        results.append({
            'area': int(area),
            'hull_area': int(hull_area),
            'ratio': ratio,
            'solidity': solidity,
            'rectangularity': rectangularity,
            'hull_rect': hull_rect,
            'oversize': area > max_area,
        })

    results.sort(key=lambda x: -x['area'])
    return results


def main():
    if card_detect._staging_roi_area and card_detect._staging_roi_area > 0:
        min_area = int(card_detect._staging_roi_area * 0.28)
        max_area = int(card_detect._staging_roi_area * 0.60)
    else:
        min_area = 80000
        max_area = 600000

    print(f"min_area={min_area}, max_area={max_area}")
    print(f"(a CARD should have area~300-500k, hull~750-900k, ratio~1.40, "
          f"hrect~0.95)")
    print()

    for rel in REAL_CARD_FRAMES:
        path = os.path.join(SCRIPT_DIR, rel.replace("/", os.sep))
        frame = cv2.imread(path)
        if frame is None:
            print(f"!! Missing {rel}")
            continue

        print("=" * 80)
        print(rel)
        print("=" * 80)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        b, g, r = cv2.split(frame)

        channel_edges = {
            "gray": cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150),
            "blue": cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), 50, 150),
            "green": cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 50, 150),
            "red": cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), 50, 150),
        }
        multi_or = cv2.bitwise_or(
            channel_edges["blue"],
            cv2.bitwise_or(channel_edges["green"], channel_edges["red"]))
        channel_edges["multi-OR"] = multi_or

        # Per channel, show top 3 candidates
        for ch_name, edges in channel_edges.items():
            results = process_channel(edges, min_area, max_area)
            if not results:
                print(f"  {ch_name:>9s}: (no candidates pass area+ROI)")
                continue
            for i, r in enumerate(results[:3]):
                over = "*OVER*" if r['oversize'] else ""
                print(f"  {ch_name:>9s}[{i}]: "
                      f"area={r['area']:>7d} hull={r['hull_area']:>7d} "
                      f"ratio={r['ratio']:.2f} "
                      f"sol={r['solidity']:.2f} "
                      f"rect={r['rectangularity']:.2f} "
                      f"hrect={r['hull_rect']:.2f} {over}")
        print()


if __name__ == "__main__":
    main()
