#!/usr/bin/env python3
"""
Test: does masking the closed edge map outside the staging ROI fix
the sprawling-contour problem for the 4 real-card failures?

Does NOT touch the raw frame (Canny still runs on full frame per the
'no frame-level ROI crop' rule). Only zeros the CLOSED EDGE map outside
the staging ROI+margin, so background clutter can't merge with card
edges via the morph's dilation.
"""

import os
import sys
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import card_detect

REAL_CARD_FRAMES = [
    ("PrickleFaeries",
     "scan_logs/session_20260421_134408/no_detect/nodet_0014_attempt1.jpg"),
    ("InvasionEldraine",
     "scan_logs/session_20260421_140152/no_detect/nodet_0008_attempt1.jpg"),
    ("InvasionZendikar",
     "scan_logs/session_20260421_140152/no_detect/nodet_0010_attempt1.jpg"),
    ("TroveTracker",
     "scan_logs/session_20260421_115519/no_detect/nodet_0121_attempt1.jpg"),
]

OUT_DIR = os.path.join(SCRIPT_DIR, "diag_out")
os.makedirs(OUT_DIR, exist_ok=True)


def build_roi_mask(shape, margin=60):
    """
    Build a binary mask for the staging ROI expanded outward by `margin`
    pixels. 0 outside, 255 inside.
    """
    h, w = shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    if card_detect._staging_roi is None:
        return None
    roi = card_detect._staging_roi.reshape(4, 1, 2).astype(np.int32)
    cv2.fillPoly(mask, [roi], 255)
    if margin > 0:
        k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * margin + 1, 2 * margin + 1))
        mask = cv2.dilate(mask, k)
    return mask


def detect_with_mask(frame, margin=60):
    """Replay detection with edge-map masking outside ROI+margin."""
    if card_detect._staging_roi_area and card_detect._staging_roi_area > 0:
        min_area = int(card_detect._staging_roi_area * 0.28)
        max_area = int(card_detect._staging_roi_area * 0.60)
    else:
        min_area, max_area = 80000, 600000

    b, g, r = cv2.split(frame)
    edges_mc = cv2.bitwise_or(
        cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), 50, 150),
        cv2.bitwise_or(
            cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 50, 150),
            cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), 50, 150),
        ),
    )

    roi_mask = build_roi_mask(frame.shape, margin=margin)
    if roi_mask is not None:
        edges_mc = cv2.bitwise_and(edges_mc, roi_mask)

    k_rect = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    k_ellipse = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    closed = cv2.bitwise_or(
        cv2.morphologyEx(edges_mc, cv2.MORPH_CLOSE, k_rect, iterations=3),
        cv2.morphologyEx(edges_mc, cv2.MORPH_CLOSE, k_ellipse, iterations=3),
    )

    contours, _ = cv2.findContours(
        closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    good = [c for c in contours if cv2.contourArea(c) >= min_area]
    good.sort(key=cv2.contourArea, reverse=True)

    return edges_mc, closed, contours, good, min_area, max_area


def metrics(cnt):
    area = cv2.contourArea(cnt)
    hull = cv2.convexHull(cnt)
    hull_area = cv2.contourArea(hull)
    solidity = area / hull_area if hull_area > 0 else 0
    rect = cv2.minAreaRect(cnt)
    rw, rh = rect[1]
    box_area = rw * rh
    ratio = max(rw, rh) / max(min(rw, rh), 1)
    rectangularity = area / box_area if box_area > 0 else 0

    peri = cv2.arcLength(cnt, True)
    poly4 = False
    for eps in (0.02, 0.04, 0.06, 0.08):
        p = cv2.approxPolyDP(cnt, eps * peri, True)
        if len(p) == 4:
            poly4 = True
            break
    return area, hull_area, ratio, solidity, rectangularity, poly4


def main():
    for name, rel in REAL_CARD_FRAMES:
        path = os.path.join(SCRIPT_DIR, rel.replace("/", os.sep))
        frame = cv2.imread(path)
        assert frame is not None, path

        print("=" * 72)
        print(name)
        print("=" * 72)

        for margin in (0, 30, 60):
            edges_mc, closed, all_c, good, min_area, max_area = (
                detect_with_mask(frame, margin=margin))
            print(f"  margin={margin:3d}: {len(good)} candidates >= min_area "
                  f"({min_area})")
            for i, c in enumerate(good[:3]):
                area, hull_area, ratio, sol, rect, poly4 = metrics(c)
                over = "*OVER*" if area > max_area else ""
                print(f"    [{i}] area={int(area):>7d} hull={int(hull_area):>7d} "
                      f"ratio={ratio:.2f} sol={sol:.2f} rect={rect:.2f} "
                      f"poly4={'Y' if poly4 else 'n'} {over}")

            # Save visualization for margin=60
            if margin == 60:
                vis = frame.copy()
                cv2.drawContours(vis, all_c, -1, (0, 255, 0), 1)
                if good:
                    cv2.drawContours(vis, [good[0]], -1, (0, 0, 255), 2)
                prefix = os.path.join(OUT_DIR, f"MASKED_{name}")
                cv2.imwrite(f"{prefix}_1_edges_masked.png", edges_mc)
                cv2.imwrite(f"{prefix}_2_closed_mc.png", closed)
                cv2.imwrite(f"{prefix}_4_top_contour.png", vis)


if __name__ == "__main__":
    main()
