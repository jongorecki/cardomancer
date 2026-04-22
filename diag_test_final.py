#!/usr/bin/env python3
"""
Final validation: 2-pass cascade at Canny 30/90 with
 - Pass A: 3x3 kernel, iter=1  (tight — card's own edges, no merge)
 - Pass B: 3x3 kernel, iter=3  (stronger close — bridges corner gaps)

With corrected real-card labels (7 real frames).
"""

import os
import sys
import glob
import cv2

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import card_detect

# Corrected after inspecting each frame
REAL_CARD_FRAMES = {
    "scan_logs/session_20260421_115519/no_detect/nodet_0048_attempt1.jpg",        # Elvish Doomsayer
    "scan_logs/session_20260421_115519/no_detect/nodet_0055_attempt2.jpg",        # Supernatural Stamina
    "scan_logs/session_20260421_115519/no_detect/nodet_0121_attempt1.jpg",        # Trove Tracker
    "scan_logs/session_20260421_134408/no_detect/nodet_0014_attempt1.jpg",        # Prickle Faeries
    "scan_logs/session_20260421_140152/no_detect/nodet_0008_attempt1.jpg",        # Invasion of Eldraine
    "scan_logs/session_20260421_140152/no_detect/nodet_0010_attempt1.jpg",        # Invasion of Zendikar
    "scan_logs/session_20260421_140152/no_detect/nodet_0076_attempt1.jpg",        # Talarian Contempt
}

NO_DETECT_GLOB = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_*", "no_detect", "nodet_*.jpg")


def try_pass(frame, canny_low, canny_high, kernel_size, morph_iter,
             min_area, max_area):
    """Return True if a card is detected under these params."""
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

        if abs(ratio - 1.396) / 1.396 > 0.35:
            continue
        if solidity < 0.65:
            continue
        if rectangularity < 0.70:
            continue

        peri = cv2.arcLength(cnt, True)
        for eps in (0.02, 0.04, 0.06, 0.08):
            p = cv2.approxPolyDP(cnt, eps * peri, True)
            if len(p) == 4:
                return True
    return False


def detect_2pass_v2(frame):
    """
    Two-pass cascade at Canny 30/90:
      Pass 1: 3x3 kernel, iter=1
      Pass 2: 3x3 kernel, iter=3
    """
    if card_detect._staging_roi_area and card_detect._staging_roi_area > 0:
        min_area = int(card_detect._staging_roi_area * 0.28)
        max_area = int(card_detect._staging_roi_area * 0.60)
    else:
        min_area, max_area = 80000, 600000

    if try_pass(frame, 30, 90, 3, 1, min_area, max_area):
        return "pass1"
    if try_pass(frame, 30, 90, 3, 3, min_area, max_area):
        return "pass2"
    return None


def main():
    frames = sorted(glob.glob(NO_DETECT_GLOB))
    real_hits = 0
    real_missed = []
    empty_rejected = 0
    empty_fps = []

    for p in frames:
        rel = os.path.relpath(p, SCRIPT_DIR).replace("\\", "/")
        frame = cv2.imread(p)
        result = detect_2pass_v2(frame)
        is_real = rel in REAL_CARD_FRAMES

        if is_real:
            if result:
                real_hits += 1
                print(f"  [OK ] REAL    {result}  {os.path.basename(rel)}")
            else:
                real_missed.append(rel)
                print(f"  [!! ] REAL MISSED    {os.path.basename(rel)}")
        else:
            if result:
                empty_fps.append((rel, result))
                print(f"  [? ] empty  {result}  {os.path.basename(rel)}")
            else:
                empty_rejected += 1

    print()
    print("=" * 72)
    print(f"Real cards detected: {real_hits}/{len(REAL_CARD_FRAMES)}")
    print(f"Empty platforms correctly rejected: {empty_rejected}/"
          f"{len(frames) - len(REAL_CARD_FRAMES)}")
    if empty_fps:
        print(f"Suspected empty FPs: {len(empty_fps)}")
        for rel, which in empty_fps:
            print(f"  {which:6s} {rel}")
    if real_missed:
        print(f"Real cards still missed: {len(real_missed)}")
        for rel in real_missed:
            print(f"  {rel}")


if __name__ == "__main__":
    main()
