#!/usr/bin/env python3
# diagnose_scan_290.py
# Scan 290 was detected in baseline but became no-detect after refinement.
# Figure out what happened.

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
OUT_DIR = os.path.join(SCRIPT_DIR, "diag_290")
os.makedirs(OUT_DIR, exist_ok=True)


def main():
    for scan_num in [290]:
        frame_path = os.path.join(FRAMES_DIR, f"scan_{scan_num:04d}.jpg")
        frame = cv2.imread(frame_path)
        if frame is None:
            print(f"scan {scan_num}: frame not found")
            continue

        if cd._staging_roi_area and cd._staging_roi_area > 0:
            min_area = int(cd._staging_roi_area * 0.28)
            max_area = int(cd._staging_roi_area * 0.60)
        else:
            min_area = 80000
            max_area = 600000
        print(f"min_area={min_area} max_area={max_area}")

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        b, g, r = cv2.split(frame)
        edges_gray = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)
        edges_b = cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), 50, 150)
        edges_g = cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 50, 150)
        edges_r = cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), 50, 150)
        edges_mc = cv2.bitwise_or(
            edges_b, cv2.bitwise_or(edges_g, edges_r))

        # Try each channel and report raw polygon
        result = cd._find_best_card_in_edges(
            edges_mc, min_area, max_area, debug=True)
        print(f"\nmulti-OR: {result}")
        if result is not None:
            approx, area = result
            print(f"  polygon area {area:.0f}")

        for ch_name, e in [("gray", edges_gray), ("blue", edges_b),
                           ("green", edges_g), ("red", edges_r)]:
            r2 = cd._find_best_card_in_edges(
                e, min_area, max_area, debug=False)
            if r2 is not None:
                approx, area = r2
                print(f"  {ch_name}: area {area:.0f}")
            else:
                print(f"  {ch_name}: no polygon")

        # Run the FULL detector with debug
        print("\n--- Full detect_card (with refinement) ---")
        card_img = cd.detect_card(frame, debug=True)
        print(f"detect_card returned: {card_img}")

        # Now: what if we skip the refinement?
        print("\n--- Without refinement ---")
        contour = cd._find_card_contour(frame, debug=False)
        if contour is not None:
            print(f"pre-refine polygon found, area "
                  f"{cv2.contourArea(contour.reshape(4,2).astype(np.int32)):.0f}")
        else:
            print("Even pre-refine polygon is None")

        # Save visualizations
        vis = frame.copy()
        if result is not None:
            approx, _ = result
            cv2.drawContours(vis, [approx], -1, (0, 0, 255), 3)
        cv2.imwrite(os.path.join(OUT_DIR, f"scan_{scan_num:04d}_raw.jpg"), vis)

        # Try refinement manually
        if result is not None:
            approx, _ = result
            # Log debug info for refinement
            print("\n--- Refinement debug ---")
            refined = cd._refine_polygon_outward(
                frame, approx, search_band=40, debug=True)
            print(f"refined: shape={refined.shape}")
            ra = cv2.contourArea(refined.reshape(4, 2).astype(np.int32))
            print(f"refined area: {ra:.0f}")

            vis2 = frame.copy()
            cv2.drawContours(vis2, [approx], -1, (0, 0, 255), 3)
            cv2.drawContours(
                vis2, [refined.astype(np.int32)], -1, (0, 255, 0), 3)
            cv2.imwrite(
                os.path.join(OUT_DIR, f"scan_{scan_num:04d}_refined.jpg"), vis2)


if __name__ == '__main__':
    main()
