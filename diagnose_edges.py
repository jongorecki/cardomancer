#!/usr/bin/env python3
# diagnose_edges.py
# Visualize Canny output for the problem scans to see if the outer edge
# is present-but-fragmented, or missing entirely.

import os
import sys
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
FRAMES_DIR = os.path.join(SESSION_DIR, "scan_images")
OUT_DIR = os.path.join(SCRIPT_DIR, "diag_edges")
os.makedirs(OUT_DIR, exist_ok=True)

SCANS = [27, 143, 193, 241, 271, 100]


def main():
    for scan_num in SCANS:
        frame_path = os.path.join(FRAMES_DIR, f"scan_{scan_num:04d}.jpg")
        frame = cv2.imread(frame_path)
        if frame is None:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        b, g, r = cv2.split(frame)

        # Same Canny params as card_detect.py
        edges_g = cv2.Canny(cv2.GaussianBlur(gray, (3,3), 0), 50, 150)
        edges_b = cv2.Canny(cv2.GaussianBlur(b, (3,3), 0), 50, 150)
        edges_gg = cv2.Canny(cv2.GaussianBlur(g, (3,3), 0), 50, 150)
        edges_r = cv2.Canny(cv2.GaussianBlur(r, (3,3), 0), 50, 150)

        # Multi-channel OR (primary strategy)
        edges_mc = cv2.bitwise_or(edges_b, cv2.bitwise_or(edges_gg, edges_r))

        # Current morph close
        k_rect = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        k_elli = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        closed_r = cv2.morphologyEx(edges_mc, cv2.MORPH_CLOSE, k_rect, iterations=2)
        closed_e = cv2.morphologyEx(edges_mc, cv2.MORPH_CLOSE, k_elli, iterations=2)
        closed_5 = cv2.bitwise_or(closed_r, closed_e)

        # Alternative: bigger kernel + more iterations
        k_big = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        closed_9 = cv2.morphologyEx(edges_mc, cv2.MORPH_CLOSE, k_big, iterations=3)

        # Compose 2x2 grid: raw frame | raw edges | current closed | stronger closed
        h, w = frame.shape[:2]
        target_h = 600
        scale = target_h / h
        tw = int(w * scale)

        def to_bgr(img):
            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img

        panels = []
        for img, label in [
            (frame, "original"),
            (edges_mc, "Canny (multi-OR)"),
            (closed_5, "current: 5x5 close x2"),
            (closed_9, "stronger: 9x9 close x3"),
        ]:
            p = cv2.resize(to_bgr(img), (tw, target_h))
            cv2.putText(p, label, (10, 30),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            panels.append(p)

        top = np.hstack([panels[0], panels[1]])
        bot = np.hstack([panels[2], panels[3]])
        combined = np.vstack([top, bot])

        cv2.putText(combined, f"scan {scan_num}", (10, target_h*2 - 10),
                   cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)

        out = os.path.join(OUT_DIR, f"edges_{scan_num:04d}.jpg")
        cv2.imwrite(out, combined)
        print(f"  scan {scan_num}: {out}")

    print(f"\nOutput: {OUT_DIR}")


if __name__ == '__main__':
    main()
