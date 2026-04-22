#!/usr/bin/env python3
"""
Regression test: run the new 2-pass detector on all successfully-detected
raw frames from recent sessions. Any we currently detect should still be
detected by the new cascade.
"""

import os
import sys
import glob
import cv2

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# Import our tested detector
from diag_test_final import detect_2pass_v2, try_pass

# Also compare against CURRENT card_detect.detect_card
import card_detect


SCAN_IMG_GLOB = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_2026042*", "scan_images", "*.jpg")


def main():
    frames = sorted(glob.glob(SCAN_IMG_GLOB))
    print(f"Testing against {len(frames)} successful scan frames...")

    current_ok = 0
    new_ok = 0
    regressions = []
    recoveries = []

    for p in frames:
        frame = cv2.imread(p)
        if frame is None:
            continue

        # Current detector
        old_result = card_detect.detect_card(frame)
        old_ok = old_result is not None

        # New 2-pass
        new_result = detect_2pass_v2(frame)
        new_ok_bool = new_result is not None

        if old_ok:
            current_ok += 1
        if new_ok_bool:
            new_ok += 1

        if old_ok and not new_ok_bool:
            regressions.append(p)
        elif not old_ok and new_ok_bool:
            recoveries.append((p, new_result))

    print()
    print(f"Current detector: {current_ok}/{len(frames)} successful "
          f"({100*current_ok/len(frames):.1f}%)")
    print(f"New 2-pass:       {new_ok}/{len(frames)} successful "
          f"({100*new_ok/len(frames):.1f}%)")
    print()
    if regressions:
        print(f"REGRESSIONS (new misses ones old caught): {len(regressions)}")
        for p in regressions[:10]:
            print(f"  {os.path.relpath(p, SCRIPT_DIR)}")
    else:
        print("No regressions — new detector catches everything current one did.")
    if recoveries:
        print(f"Recoveries (new catches ones old missed): {len(recoveries)}")
        for p, which in recoveries[:10]:
            print(f"  {which}  {os.path.relpath(p, SCRIPT_DIR)}")


if __name__ == "__main__":
    main()
