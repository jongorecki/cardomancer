#!/usr/bin/env python3
# test_regression_362.py
# ---------------------------------------------------------------------------
# Full-pipeline regression test on the corrected 362-card session:
#   camera frame -> detect_card() -> identify_card() (hybrid)
# Compares against the CORRECTED CSV names.
# ---------------------------------------------------------------------------

import csv
import os
import sys
import time
import cv2

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

print("Loading modules...")
from card_detect import detect_card
from card_identify_hybrid import identify_card, is_card_back
from cards import CARD_DATA_BY_ID

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
CSV_PATH = os.path.join(SESSION_DIR, "scans.csv")
FRAMES_DIR = os.path.join(SESSION_DIR, "scan_images")


def get_name(card_id):
    if not card_id:
        return "(no match)"
    base = card_id.replace("__back", "")
    entry = CARD_DATA_BY_ID.get(base)
    name = entry.get('name', '???') if entry else '???'
    if '__back' in card_id:
        name += " (back)"
    return name


def names_match(a, b):
    if not a or not b:
        return False
    a = a.lower().strip()
    b = b.lower().strip()
    if a == b:
        return True
    if ' // ' in a and a.split(' // ')[0].strip() == b:
        return True
    if ' // ' in b and b.split(' // ')[0].strip() == a:
        return True
    if a in b or b in a:
        return True
    return False


def main():
    with open(CSV_PATH, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    detected = 0
    matched = 0
    correct = 0
    wrong = 0
    no_match = 0
    no_detect = 0
    back_detected = 0

    mismatches = []
    no_detects = []

    t0 = time.time()
    for i, row in enumerate(rows):
        scan_num = int(row['scan_num'])
        csv_name = row['name']
        frame_path = os.path.join(FRAMES_DIR, f"scan_{scan_num:04d}.jpg")
        if not os.path.exists(frame_path):
            continue

        frame = cv2.imread(frame_path)
        if frame is None:
            continue

        # Detection
        card_img = detect_card(frame)
        if card_img is None:
            no_detect += 1
            no_detects.append((scan_num, csv_name))
            continue
        detected += 1

        # Card back check
        is_back, back_dist = is_card_back(card_img)
        if is_back:
            back_detected += 1
            if csv_name.lower() in ('card back', '(card back)', 'back', 'unrecognized'):
                correct += 1
            continue

        # Identification
        card_id, dist, rotated, _ = identify_card(card_img)
        if card_id is None:
            no_match += 1
            mismatches.append({
                'scan': scan_num, 'csv': csv_name,
                'got': '(no match)', 'dist': dist,
            })
            continue
        matched += 1

        got_name = get_name(card_id)
        if names_match(csv_name, got_name):
            correct += 1
        else:
            wrong += 1
            mismatches.append({
                'scan': scan_num, 'csv': csv_name,
                'got': got_name, 'dist': dist,
            })

        if (i + 1) % 50 == 0 or (i + 1) == total:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (total - i - 1) / rate if rate > 0 else 0
            print(f"  [{i+1:3d}/{total}] {elapsed:.0f}s elapsed, "
                  f"~{eta:.0f}s remaining "
                  f"(correct: {correct}, wrong: {wrong}, "
                  f"no-det: {no_detect}, backs: {back_detected})")

    elapsed = time.time() - t0

    print("\n" + "=" * 80)
    print("REGRESSION TEST RESULTS — session_20260415_130451")
    print("=" * 80)
    print(f"Total scans:         {total}")
    print(f"Detected:            {detected}/{total} ({100*detected/total:.1f}%)")
    print(f"No-detect:           {no_detect}/{total} ({100*no_detect/total:.1f}%)")
    print(f"Card backs detected: {back_detected}")
    print(f"Identified (match):  {matched}/{detected} "
          f"({100*matched/max(detected,1):.1f}%)")
    print(f"No-match (below threshold): {no_match}")
    print(f"Correct:             {correct}/{total} "
          f"({100*correct/total:.1f}%)")
    print(f"Wrong:               {wrong}/{total}")
    print(f"Total time:          {elapsed:.1f}s ({elapsed/total:.2f}s/card)")

    if no_detects:
        print(f"\nNO-DETECT ({len(no_detects)}):")
        for s, n in no_detects:
            print(f"  scan {s:4d}: {n}")

    if mismatches:
        print(f"\nMISMATCHES ({len(mismatches)}):")
        print(f"  {'Scan':>4}  {'Dist':>6}  "
              f"{'CSV Name':<35}  {'Got Name':<35}")
        print("  " + "-" * 82)
        for m in sorted(mismatches, key=lambda x: x['scan']):
            print(f"  {m['scan']:4d}  {m['dist']:6.1f}  "
                  f"{m['csv'][:33]:<35}  {m['got'][:33]:<35}")


if __name__ == '__main__':
    main()
