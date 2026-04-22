#!/usr/bin/env python3
"""
Diagnose why card_detect.py rejects frames saved in scan_logs/*/no_detect/.

Runs detect_card(debug=True) on each frame and captures the debug output
per frame so we can see exactly which filter rejected each card.
"""

import os
import io
import sys
import glob
import contextlib

import cv2

# Make sure we import from Scripts/ so we pick up the real card_detect
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import card_detect


NO_DETECT_GLOB = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_*", "no_detect", "nodet_*.jpg")


def run_one(path):
    frame = cv2.imread(path)
    if frame is None:
        return f"    [!] could not read {path}", None

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        contour = card_detect._find_card_contour(frame, debug=True)

    debug = buf.getvalue().strip()
    return debug, contour


def main():
    frames = sorted(glob.glob(NO_DETECT_GLOB))
    if not frames:
        print(f"No frames found under {NO_DETECT_GLOB}")
        return

    print(f"Found {len(frames)} no-detect frames.\n")

    # Categorize rejection reasons
    reasons = {
        "area_too_large": 0,
        "outside_roi": 0,
        "solidity_low": 0,
        "ratio_bad": 0,
        "rectangularity_low": 0,
        "no_contour_at_all": 0,
        "detected_after_rerun": 0,
        "other": 0,
    }
    rejections_per_frame = []

    for path in frames:
        name = os.path.relpath(path, SCRIPT_DIR)
        debug, contour = run_one(path)

        if contour is not None:
            reasons["detected_after_rerun"] += 1
            rejections_per_frame.append((name, "DETECTED (inconsistent)", debug))
            continue

        # Count rejection types in the debug text
        frame_reasons = []
        if "Rejected: area" in debug:
            reasons["area_too_large"] += 1
            frame_reasons.append("area_too_large")
        if "outside staging ROI" in debug:
            reasons["outside_roi"] += 1
            frame_reasons.append("outside_roi")
        if "solidity" in debug:
            reasons["solidity_low"] += 1
            frame_reasons.append("solidity_low")
        if "ratio" in debug and "vs expected" in debug:
            reasons["ratio_bad"] += 1
            frame_reasons.append("ratio_bad")
        if "rectangularity" in debug:
            reasons["rectangularity_low"] += 1
            frame_reasons.append("rectangularity_low")
        if "No card contour found" in debug and not frame_reasons:
            reasons["no_contour_at_all"] += 1
            frame_reasons.append("no_contour_at_all")
        if not frame_reasons:
            reasons["other"] += 1
            frame_reasons.append("other")

        rejections_per_frame.append((name, ",".join(frame_reasons), debug))

    # Summary
    print("=" * 72)
    print("SUMMARY (counts are per-frame occurrences; some frames have multiple):")
    print("=" * 72)
    for k, v in reasons.items():
        print(f"  {k:25s}: {v}")
    print()

    # Per-frame detail
    print("=" * 72)
    print("PER-FRAME DETAIL:")
    print("=" * 72)
    for name, summary, debug in rejections_per_frame:
        print(f"\n{name}")
        print(f"  -> {summary}")
        for line in debug.splitlines():
            print(f"     {line}")


if __name__ == "__main__":
    main()
