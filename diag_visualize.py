#!/usr/bin/env python3
"""
Visualize what card_detect sees for a real-card no-detect frame.

Saves:
  <out>_0_frame.png         - original frame
  <out>_1_edges_mc.png      - multi-OR Canny edges
  <out>_2_closed_mc.png     - after dual morph close
  <out>_3_contours_all.png  - all contours drawn on frame
  <out>_4_top_contour.png   - just the largest candidate contour
"""

import os
import sys
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import card_detect  # noqa

REAL_CARD_FRAMES = [
    ("nodet_0014_PrickleFaeries",
     "scan_logs/session_20260421_134408/no_detect/nodet_0014_attempt1.jpg"),
    ("nodet_0008_InvasionEldraine",
     "scan_logs/session_20260421_140152/no_detect/nodet_0008_attempt1.jpg"),
    ("nodet_0010_InvasionZendikar",
     "scan_logs/session_20260421_140152/no_detect/nodet_0010_attempt1.jpg"),
    ("nodet_0121_TroveTracker",
     "scan_logs/session_20260421_115519/no_detect/nodet_0121_attempt1.jpg"),
]

OUT_DIR = os.path.join(SCRIPT_DIR, "diag_out")
os.makedirs(OUT_DIR, exist_ok=True)


def process(name, rel_path):
    path = os.path.join(SCRIPT_DIR, rel_path.replace("/", os.sep))
    frame = cv2.imread(path)
    assert frame is not None, path

    b, g, r = cv2.split(frame)
    edges_mc = cv2.bitwise_or(
        cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), 50, 150),
        cv2.bitwise_or(
            cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 50, 150),
            cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), 50, 150),
        ),
    )
    k_rect = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    k_ellipse = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    closed = cv2.bitwise_or(
        cv2.morphologyEx(edges_mc, cv2.MORPH_CLOSE, k_rect, iterations=3),
        cv2.morphologyEx(edges_mc, cv2.MORPH_CLOSE, k_ellipse, iterations=3),
    )

    contours, _ = cv2.findContours(
        closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if card_detect._staging_roi_area and card_detect._staging_roi_area > 0:
        min_area = int(card_detect._staging_roi_area * 0.28)
        max_area = int(card_detect._staging_roi_area * 0.60)
    else:
        min_area, max_area = 80000, 600000

    # Filter to contours passing area
    good = [c for c in contours if cv2.contourArea(c) >= min_area]
    good.sort(key=cv2.contourArea, reverse=True)

    # All contours visualization
    vis_all = frame.copy()
    cv2.drawContours(vis_all, contours, -1, (0, 255, 0), 1)

    # Top candidate + hull + bounding rect
    vis_top = frame.copy()
    if good:
        top = good[0]
        cv2.drawContours(vis_top, [top], -1, (0, 255, 0), 2)  # contour green
        hull = cv2.convexHull(top)
        cv2.drawContours(vis_top, [hull], -1, (0, 0, 255), 2)  # hull red
        rect = cv2.minAreaRect(top)
        box = cv2.boxPoints(rect)
        box = np.intp(box)
        cv2.drawContours(vis_top, [box], -1, (255, 0, 0), 2)  # bbox blue

    prefix = os.path.join(OUT_DIR, name)
    cv2.imwrite(f"{prefix}_0_frame.png", frame)
    cv2.imwrite(f"{prefix}_1_edges_mc.png", edges_mc)
    cv2.imwrite(f"{prefix}_2_closed_mc.png", closed)
    cv2.imwrite(f"{prefix}_3_contours_all.png", vis_all)
    cv2.imwrite(f"{prefix}_4_top_contour.png", vis_top)
    print(f"[{name}] wrote to {prefix}_*.png  ({len(good)} contours >= min_area)")


def main():
    for name, rel in REAL_CARD_FRAMES:
        process(name, rel)


if __name__ == "__main__":
    main()
