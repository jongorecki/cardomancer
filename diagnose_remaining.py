#!/usr/bin/env python3
# diagnose_remaining.py
# For each remaining real-identification-error scan, save the detected
# crop AND the top-5 hash+DINO matches to understand what's going wrong.

import os
import sys
import cv2
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

print("Loading...")
from card_detect import detect_card
from card_identify_hybrid import identify_card, is_card_back
from cards import CARD_DATA_BY_ID

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
FRAMES_DIR = os.path.join(SESSION_DIR, "scan_images")
OUT_DIR = os.path.join(SCRIPT_DIR, "diag_remaining")
os.makedirs(OUT_DIR, exist_ok=True)

# Scans with real identification errors
SCANS = [27, 140, 143, 205, 217, 290, 299]


def get_name(card_id):
    if not card_id:
        return "(no match)"
    base = card_id.replace("__back", "")
    e = CARD_DATA_BY_ID.get(base)
    return (e.get('name', '???') if e else '???') + \
        (" (back)" if '__back' in card_id else '')


def main():
    for s in SCANS:
        frame_path = os.path.join(FRAMES_DIR, f"scan_{s:04d}.jpg")
        frame = cv2.imread(frame_path)
        if frame is None:
            continue

        card_img = detect_card(frame)
        if card_img is None:
            print(f"scan {s}: NO DETECT")
            continue

        # Save crop
        cv2.imwrite(os.path.join(OUT_DIR, f"crop_{s:04d}.jpg"), card_img)

        # Identify
        cid, dist, rotated, all_results = identify_card(card_img)
        print(f"\nscan {s}: best match = {get_name(cid)} (dist={dist:.1f}, rotated={rotated})")
        if all_results and isinstance(all_results, dict):
            # Common return: {'hash': [...], 'dino': [...]} or similar
            for k, v in all_results.items():
                print(f"  [{k}] top 5:")
                if hasattr(v, '__iter__'):
                    items = list(v)[:5]
                    for it in items:
                        if isinstance(it, (list, tuple)) and len(it) >= 2:
                            nm = get_name(it[0])
                            print(f"    dist={it[1]:.1f}  {nm}")
                        else:
                            print(f"    {it!r}")
        elif all_results:
            print(f"  results: {all_results!r}")


if __name__ == '__main__':
    main()
