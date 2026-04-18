#!/usr/bin/env python3
# diagnose_refinement.py
# Visualize original (inner-frame) vs refined (outer-edge) polygon on a
# handful of scans. Writes side-by-side comparison images so we can confirm
# the refinement is snapping outward to the real card edge.

import os
import sys
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import card_detect as cd

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
FRAMES_DIR = os.path.join(SESSION_DIR, "scan_images")
OUT_DIR = os.path.join(SCRIPT_DIR, "diag_refinement")
os.makedirs(OUT_DIR, exist_ok=True)

# Mix of cards that were cropped to inner border, and some control cases
SCANS = [27, 96, 118, 140, 143, 193, 205, 215, 216, 271, 299, 317, 356,
         50, 100, 241]


def draw_polygon(img, poly, color, thickness=3):
    pts = poly.reshape(-1, 2).astype(np.int32)
    for i in range(4):
        p0 = tuple(pts[i])
        p1 = tuple(pts[(i + 1) % 4])
        cv2.line(img, p0, p1, color, thickness)
    for i in range(4):
        cv2.circle(img, tuple(pts[i]), 8, color, -1)


def zoom_crop(img, poly, pad=60):
    pts = poly.reshape(-1, 2).astype(np.int32)
    x, y, w, h = cv2.boundingRect(pts)
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(img.shape[1], x + w + pad)
    y1 = min(img.shape[0], y + h + pad)
    return img[y0:y1, x0:x1], (x0, y0)


def main():
    for scan_num in SCANS:
        frame_path = os.path.join(FRAMES_DIR, f"scan_{scan_num:04d}.jpg")
        frame = cv2.imread(frame_path)
        if frame is None:
            print(f"scan {scan_num}: frame not found")
            continue

        # 1) Run the _find_card_contour WITHOUT refinement by monkey-patching
        #    temporarily — simplest way: call the internal helpers directly
        #    to get the unrefined polygon, then refine.

        # Compute raw polygon (copy of _find_card_contour logic up to
        # refinement call)
        if cd._staging_roi_area and cd._staging_roi_area > 0:
            min_area = int(cd._staging_roi_area * 0.28)
            max_area = int(cd._staging_roi_area * 0.60)
        else:
            min_area = 80000
            max_area = 600000

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        b, g, r = cv2.split(frame)
        edges_gray = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)
        edges_b = cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), 50, 150)
        edges_g = cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 50, 150)
        edges_r = cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), 50, 150)
        edges_mc = cv2.bitwise_or(
            edges_b, cv2.bitwise_or(edges_g, edges_r))

        result = cd._find_best_card_in_edges(
            edges_mc, min_area, max_area, debug=False)
        if result is None:
            # Fallback per-channel
            candidates = []
            for ch_name, e in [("gray", edges_gray), ("blue", edges_b),
                               ("green", edges_g), ("red", edges_r)]:
                r2 = cd._find_best_card_in_edges(
                    e, min_area, max_area, debug=False)
                if r2 is not None:
                    approx, area = r2
                    candidates.append((approx, area))
            if not candidates:
                print(f"scan {scan_num}: no polygon found")
                continue
            areas = [a for _, a in candidates]
            med = float(np.median(areas))
            best_approx = None
            best_area = 0
            for approx, area in candidates:
                if len(candidates) > 1 and area > med * 1.15:
                    continue
                if area > best_area:
                    best_area = area
                    best_approx = approx
            raw_poly = best_approx
        else:
            raw_poly, _ = result

        if raw_poly is None:
            print(f"scan {scan_num}: no polygon (even in fallback)")
            continue

        # Refine
        refined_poly = cd._refine_polygon_outward(
            frame, raw_poly, search_band=40, debug=False)

        raw_area = cv2.contourArea(raw_poly.reshape(4, 2).astype(np.int32))
        ref_area = cv2.contourArea(
            refined_poly.reshape(4, 2).astype(np.int32))
        ratio = ref_area / raw_area if raw_area > 0 else 0

        # Draw both polygons on a zoomed crop
        vis = frame.copy()
        draw_polygon(vis, raw_poly, (0, 0, 255), thickness=3)       # red = raw
        draw_polygon(vis, refined_poly, (0, 255, 0), thickness=3)   # green = refined

        zoomed, (x0, y0) = zoom_crop(vis, raw_poly, pad=80)
        cv2.putText(
            zoomed,
            f"scan {scan_num} raw={raw_area:.0f} ref={ref_area:.0f} r={ratio:.3f}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        cv2.putText(zoomed, "RED = raw (inner)", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        cv2.putText(zoomed, "GREEN = refined (outer)", (10, 85),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        out_path = os.path.join(OUT_DIR, f"refine_{scan_num:04d}.jpg")
        cv2.imwrite(out_path, zoomed)
        print(f"scan {scan_num}: raw {raw_area:7.0f}  ref {ref_area:7.0f}  "
              f"ratio {ratio:.3f}  -> {out_path}")


if __name__ == '__main__':
    main()
