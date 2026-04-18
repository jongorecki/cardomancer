#!/usr/bin/env python3
"""
Test OCR title reading on existing scan sessions.
For each scan, detect card, warp it, OCR the title, compare to CSV ground truth.
Tests both orientations (upright and 180-rotated) and picks the one with more text.
"""

import os
import sys
import csv
import time
import cv2
import numpy as np

# Add script dir to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

from ocr import ocr_title, ocr_collector, parse_collector_info, crop_region
from card_detect import detect_card
from config import TITLE_REGION, COLLECTOR_REGION
from difflib import SequenceMatcher

SESSIONS = [
    ("session_20260413_144226", 89),
    ("session_20260413_134000", 71),
]


def normalize_name(name):
    """Normalize card name for comparison."""
    if not name:
        return ""
    # Remove non-alpha, lowercase
    return ''.join(c.lower() for c in name if c.isalpha())


def fuzzy_match(ocr_text, csv_name):
    """Check if OCR text fuzzy-matches the CSV name."""
    if not ocr_text or not csv_name:
        return False, 0.0

    # Clean OCR text
    ocr_clean = ocr_text.strip()
    csv_clean = csv_name.strip()

    # Exact match (case-insensitive)
    if ocr_clean.lower() == csv_clean.lower():
        return True, 1.0

    # Check if CSV name is contained in OCR text
    if csv_clean.lower() in ocr_clean.lower():
        return True, 0.95

    # Fuzzy ratio
    ratio = SequenceMatcher(None,
                           normalize_name(ocr_clean),
                           normalize_name(csv_clean)).ratio()
    return ratio >= 0.6, ratio


def main():
    total_scans = 0
    total_detected = 0
    total_ocr_got_text = 0
    total_ocr_match = 0
    total_ocr_time = 0
    all_results = []

    for session_name, expected_count in SESSIONS:
        session_dir = os.path.join(SCRIPT_DIR, "scan_logs", session_name)
        csv_path = os.path.join(session_dir, "scans.csv")
        img_dir = os.path.join(session_dir, "scan_images")

        if not os.path.exists(csv_path):
            print(f"SKIP: {csv_path} not found")
            continue

        # Load CSV ground truth
        ground_truth = {}
        with open(csv_path, 'r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                sn = int(row['scan_num'])
                ground_truth[sn] = row['name']

        print(f"\nTesting {session_name} ({len(ground_truth)} scans)...")
        print(f"{'Scan':>4}  {'OCR Title':<35} {'CSV Name':<30} {'Match':>5} {'Ratio':>5} {'ms':>4}")
        print("-" * 90)

        for scan_num in sorted(ground_truth.keys()):
            csv_name = ground_truth[scan_num]
            total_scans += 1

            # Find scan image
            # Try both extensions
            img_path = os.path.join(img_dir, f"scan_{scan_num:04d}.jpg")
            if not os.path.exists(img_path):
                img_path = os.path.join(img_dir, f"scan_{scan_num:04d}.png")
            if not os.path.exists(img_path):
                print(f"{scan_num:>4}  {'NO_IMAGE':<35} {csv_name:<30}")
                continue

            # Detect and warp card
            frame = cv2.imread(img_path)
            if frame is None:
                print(f"{scan_num:>4}  {'READ_FAIL':<35} {csv_name:<30}")
                continue

            card_img = detect_card(frame)
            if card_img is None:
                print(f"{scan_num:>4}  {'NO_DETECT':<35} {csv_name:<30}")
                continue

            total_detected += 1

            # OCR title in both orientations
            t0 = time.time()

            title_up = ocr_title(card_img)
            card_rot = cv2.rotate(card_img, cv2.ROTATE_180)
            title_rot = ocr_title(card_rot)

            elapsed_ms = (time.time() - t0) * 1000
            total_ocr_time += elapsed_ms

            # Pick orientation with more alpha chars
            alpha_up = sum(c.isalpha() for c in (title_up or ""))
            alpha_rot = sum(c.isalpha() for c in (title_rot or ""))

            if alpha_rot > alpha_up:
                title = title_rot
                rotated = True
            else:
                title = title_up
                rotated = False

            if title and len(title.strip()) > 0:
                total_ocr_got_text += 1

            is_match, ratio = fuzzy_match(title, csv_name)
            if is_match:
                total_ocr_match += 1

            # Truncate for display
            title_disp = (title or "")[:35]
            csv_disp = csv_name[:30]
            rot_marker = "R" if rotated else " "
            match_marker = "YES" if is_match else "NO"
            arrow = "" if is_match else " <--"

            print(f"{scan_num:>4}{rot_marker} {title_disp:<35} {csv_disp:<30} {match_marker:>5} {ratio:>5.2f} {elapsed_ms:>4.0f}{arrow}")

            all_results.append({
                'session': session_name,
                'scan': scan_num,
                'csv_name': csv_name,
                'ocr_title': title,
                'rotated': rotated,
                'match': is_match,
                'ratio': ratio,
                'time_ms': elapsed_ms,
            })

    # Summary
    print("\n" + "=" * 90)
    print(f"OVERALL: {total_scans} scans")
    print(f"  Detected:     {total_detected}/{total_scans} ({100*total_detected/max(1,total_scans):.1f}%)")
    print(f"  Got OCR text: {total_ocr_got_text}/{total_detected} ({100*total_ocr_got_text/max(1,total_detected):.1f}%)")
    print(f"  Name match:   {total_ocr_match}/{total_detected} ({100*total_ocr_match/max(1,total_detected):.1f}%)")
    print(f"  Avg OCR time: {total_ocr_time/max(1,total_detected):.0f}ms per card (both orientations)")

    # Show mismatches
    mismatches = [r for r in all_results if not r['match'] and r['ocr_title']]
    if mismatches:
        print(f"\nMISMATCHES ({len(mismatches)}):")
        for r in mismatches:
            print(f"  {r['session'][-6:]} scan {r['scan']:>2}: "
                  f"OCR='{r['ocr_title'][:40]}' vs CSV='{r['csv_name'][:30]}' "
                  f"(ratio={r['ratio']:.2f})")


if __name__ == "__main__":
    main()
