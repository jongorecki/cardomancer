#!/usr/bin/env python3
# diagnose_edges_zoom.py
# Zoomed-in view of the edge maps around just the card area.

import os
import sys
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from card_detect import _find_card_contour

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
FRAMES_DIR = os.path.join(SESSION_DIR, "scan_images")
OUT_DIR = os.path.join(SCRIPT_DIR, "diag_edges_zoom")
os.makedirs(OUT_DIR, exist_ok=True)

SCANS = [193, 241, 271, 100]


def main():
    for scan_num in SCANS:
        frame_path = os.path.join(FRAMES_DIR, f"scan_{scan_num:04d}.jpg")
        frame = cv2.imread(frame_path)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        b, g, r = cv2.split(frame)
        edges_mc = cv2.bitwise_or(
            cv2.Canny(cv2.GaussianBlur(b, (3,3), 0), 50, 150),
            cv2.bitwise_or(
                cv2.Canny(cv2.GaussianBlur(g, (3,3), 0), 50, 150),
                cv2.Canny(cv2.GaussianBlur(r, (3,3), 0), 50, 150),
            ),
        )

        k5 = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        k9 = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        closed_5 = cv2.morphologyEx(edges_mc, cv2.MORPH_CLOSE, k5, iterations=2)
        closed_9 = cv2.morphologyEx(edges_mc, cv2.MORPH_CLOSE, k9, iterations=3)

        # Use the detected contour bbox to zoom in
        contour = _find_card_contour(frame)
        if contour is None:
            continue
        pts = contour.reshape(-1, 2).astype(np.int32)
        x, y, w, h = cv2.boundingRect(pts)
        pad = 100
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(frame.shape[1], x + w + pad)
        y1 = min(frame.shape[0], y + h + pad)

        def crop_to_zoom(img):
            sub = img[y0:y1, x0:x1]
            if sub.ndim == 2:
                sub = cv2.cvtColor(sub, cv2.COLOR_GRAY2BGR)
            return cv2.resize(sub, None, fx=1.8, fy=1.8, interpolation=cv2.INTER_NEAREST)

        panels = []
        for img, label in [
            (frame, "original"),
            (edges_mc, "Canny raw"),
            (closed_5, "CURRENT: 5x5 close x2"),
            (closed_9, "STRONGER: 9x9 close x3"),
        ]:
            p = crop_to_zoom(img)
            cv2.putText(p, label, (12, 34),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 3)
            panels.append(p)

        # Pad all to same size
        target_h = max(p.shape[0] for p in panels)
        target_w = max(p.shape[1] for p in panels)
        padded = []
        for p in panels:
            ph, pw = p.shape[:2]
            canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
            canvas[:ph, :pw] = p
            padded.append(canvas)

        top = np.hstack([padded[0], padded[1]])
        bot = np.hstack([padded[2], padded[3]])
        out = np.vstack([top, bot])

        cv2.putText(out, f"scan {scan_num}", (20, 40),
                   cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 3)

        out_path = os.path.join(OUT_DIR, f"zoom_{scan_num:04d}.jpg")
        cv2.imwrite(out_path, out)
        print(f"  scan {scan_num}: saved {out.shape[1]}x{out.shape[0]}")


if __name__ == '__main__':
    main()
