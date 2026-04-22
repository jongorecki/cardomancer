#!/usr/bin/env python3
"""
Test: does raising Canny thresholds (to reject background noise while
still catching card borders) fix the sprawling contour issue?

Also tries reducing morph strength (to prevent cross-merging) and
RETR_LIST to see all nested contours.
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


def detect_variant(frame, low, high, morph_iter, min_area, max_area,
                   use_retr_list=False):
    b, g, r = cv2.split(frame)
    edges = cv2.bitwise_or(
        cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), low, high),
        cv2.bitwise_or(
            cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), low, high),
            cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), low, high),
        ),
    )
    k_rect = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    k_ellipse = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    closed = cv2.bitwise_or(
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_rect, iterations=morph_iter),
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k_ellipse, iterations=morph_iter),
    )
    mode = cv2.RETR_LIST if use_retr_list else cv2.RETR_EXTERNAL
    contours, _ = cv2.findContours(closed, mode, cv2.CHAIN_APPROX_SIMPLE)

    # ROI filter + area filter
    good = []
    for c in contours:
        a = cv2.contourArea(c)
        if a < min_area:
            continue
        M = cv2.moments(c)
        if M["m00"] > 0:
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]
            if not card_detect._is_inside_roi(cx, cy, margin=100):
                continue
        good.append(c)
    good.sort(key=cv2.contourArea, reverse=True)
    return good, closed


def metrics(cnt):
    a = cv2.contourArea(cnt)
    hull = cv2.convexHull(cnt)
    ha = cv2.contourArea(hull)
    sol = a / ha if ha > 0 else 0
    rect = cv2.minAreaRect(cnt)
    rw, rh = rect[1]
    ba = rw * rh
    ratio = max(rw, rh) / max(min(rw, rh), 1)
    r2 = a / ba if ba > 0 else 0

    peri = cv2.arcLength(cnt, True)
    poly4 = False
    poly_eps = 0
    for eps in (0.02, 0.04, 0.06, 0.08):
        p = cv2.approxPolyDP(cnt, eps * peri, True)
        if len(p) == 4:
            poly4 = True
            poly_eps = eps
            break
    # Try polygon on hull too
    hull_peri = cv2.arcLength(hull, True)
    hull_poly4 = False
    for eps in (0.02, 0.04, 0.06, 0.08):
        p = cv2.approxPolyDP(hull, eps * hull_peri, True)
        if len(p) == 4:
            hull_poly4 = True
            break
    return a, ha, ratio, sol, r2, poly4, poly_eps, hull_poly4


def main():
    if card_detect._staging_roi_area and card_detect._staging_roi_area > 0:
        min_area = int(card_detect._staging_roi_area * 0.28)
        max_area = int(card_detect._staging_roi_area * 0.60)
    else:
        min_area, max_area = 80000, 600000

    variants = [
        ("baseline      ", 50, 150, 3, False),
        ("canny 80/200  ", 80, 200, 3, False),
        ("canny 100/250 ", 100, 250, 3, False),
        ("canny 60/180 i1", 60, 180, 1, False),
        ("canny 100/250 i1", 100, 250, 1, False),
        ("canny 100/250 i2 LIST", 100, 250, 2, True),
    ]

    for name, rel in REAL_CARD_FRAMES:
        path = os.path.join(SCRIPT_DIR, rel.replace("/", os.sep))
        frame = cv2.imread(path)
        assert frame is not None, path

        print("=" * 80)
        print(name)
        print("=" * 80)

        for vname, low, high, mi, retrl in variants:
            good, closed = detect_variant(
                frame, low, high, mi, min_area, max_area, retrl)
            if not good:
                print(f"  {vname}: 0 candidates")
                continue
            for i, c in enumerate(good[:3]):
                a, ha, rt, sol, r2, p4, pe, hp4 = metrics(c)
                over = "*OVER*" if a > max_area else ""
                print(f"  {vname}[{i}]: "
                      f"area={int(a):>7d} hull={int(ha):>7d} ratio={rt:.2f} "
                      f"sol={sol:.2f} rect={r2:.2f} p4={'Y' if p4 else 'n'}"
                      f"@{pe:.2f} hp4={'Y' if hp4 else 'n'} {over}")
        print()


if __name__ == "__main__":
    main()
