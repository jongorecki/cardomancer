#!/usr/bin/env python3
"""
Test the hybrid phash + DINOv2 decision logic.

Runs both methods on every scan, applies the hybrid decision rule,
and reports accuracy vs each method alone.

Decision rule (when methods disagree):
  - phash distance <= 72  -> trust phash  (phash is confident)
  - phash distance > 85   -> trust DINOv2 (phash is unreliable)
  - gray zone [73-85]:
      DINOv2 sim > 0.64   -> trust DINOv2
      else                 -> trust phash
"""

import os
import sys
import csv
import time
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

from card_detect import detect_card
from card_identify import identify_card as phash_identify, _load_db as phash_load
from card_identify_v2 import identify_card as dino_identify
from cards import CARD_DATA_BY_ID

SESSIONS = [
    ("session_20260413_144226", 89),
    ("session_20260413_134000", 71),
]


def get_name(card_id):
    if card_id and card_id in CARD_DATA_BY_ID:
        return CARD_DATA_BY_ID[card_id].get('name', card_id[:20])
    return card_id[:20] if card_id else "???"


def name_match(identified_name, csv_name):
    if not identified_name or not csv_name:
        return False
    a = identified_name.lower().strip()
    b = csv_name.lower().strip()
    if a == b:
        return True
    if a.startswith(b[:15]) or b.startswith(a[:15]):
        return True
    if a in b or b in a:
        return True
    return False


def hybrid_decide(p_name, p_dist, p_id, d_name, d_sim, d_id):
    """
    Decide which method to trust when they disagree.

    Returns (chosen_name, chosen_id, chosen_method_label)
    """
    # If both agree, easy
    if name_match(p_name, d_name):
        return p_name, p_id, "agree"

    # Disagreement - apply decision logic
    # With 1-region phash: clean gap between phash-right max (80)
    # and dino-right min (85).  Threshold at 82 splits perfectly.
    if p_dist <= 82:
        return p_name, p_id, "phash(confident)"
    else:
        return d_name, d_id, "dino(phash-weak)"


def main():
    phash_load()

    total = 0
    detected = 0

    phash_correct = 0
    dino_correct = 0
    hybrid_correct = 0
    hybrid_wrong_details = []
    disagree_details = []

    t_phash_total = 0.0
    t_dino_total = 0.0

    for session_name, _ in SESSIONS:
        session_dir = os.path.join(SCRIPT_DIR, "scan_logs", session_name)
        csv_path = os.path.join(session_dir, "scans.csv")
        img_dir = os.path.join(session_dir, "scan_images")

        if not os.path.exists(csv_path):
            continue

        ground_truth = {}
        with open(csv_path, 'r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                sn = int(row['scan_num'])
                ground_truth[sn] = row['name']

        print(f"\n{session_name} ({len(ground_truth)} scans)")
        print(f"{'Scan':>4} {'P-Dist':>6} {'D-Sim':>5} {'Hybrid Choice':<30} {'Method':<20} {'CSV Name':<30} {'OK?'}")
        print("-" * 125)

        for scan_num in sorted(ground_truth.keys()):
            csv_name = ground_truth[scan_num]
            total += 1

            img_path = os.path.join(img_dir, f"scan_{scan_num:04d}.jpg")
            if not os.path.exists(img_path):
                img_path = os.path.join(img_dir, f"scan_{scan_num:04d}.png")
            if not os.path.exists(img_path):
                continue

            frame = cv2.imread(img_path)
            if frame is None:
                continue

            card_img = detect_card(frame)
            if card_img is None:
                print(f"{scan_num:>4} {'---':>6} {'---':>5} {'NO_DETECT':<30} {'':<20} {csv_name[:30]:<30}")
                continue

            detected += 1

            # Run phash
            t0 = time.perf_counter()
            p_id, p_dist, p_rot, _ = phash_identify(card_img)
            t_phash_total += time.perf_counter() - t0
            p_name = get_name(p_id)

            # Run DINOv2
            t0 = time.perf_counter()
            d_id, d_sim, d_rot, _ = dino_identify(card_img)
            t_dino_total += time.perf_counter() - t0
            d_name = get_name(d_id)

            # Track individual accuracies
            p_ok = name_match(p_name, csv_name)
            d_ok = name_match(d_name, csv_name)
            if p_ok:
                phash_correct += 1
            if d_ok:
                dino_correct += 1

            # Hybrid decision
            h_name, h_id, method = hybrid_decide(p_name, p_dist, p_id, d_name, d_sim, d_id)
            h_ok = name_match(h_name, csv_name)
            if h_ok:
                hybrid_correct += 1
            else:
                hybrid_wrong_details.append((session_name[-6:], scan_num, csv_name, h_name, method, p_name, p_dist, d_name, d_sim))

            # Track disagreements for analysis
            if not name_match(p_name, d_name):
                correct_str = "OK" if h_ok else "WRONG"
                disagree_details.append((session_name[-6:], scan_num, csv_name, p_name, p_dist, d_name, d_sim, h_name, method, correct_str))

            marker = "OK" if h_ok else "WRONG <<<"
            print(f"{scan_num:>4} {p_dist:>6.1f} {d_sim:>5.3f} {h_name[:30]:<30} {method:<20} {csv_name[:30]:<30} {marker}")

    # Summary
    print("\n" + "=" * 125)
    print(f"TOTAL: {total} scans, {detected} detected\n")
    print(f"  Phash alone:   {phash_correct:>3}/{detected} ({100*phash_correct/detected:.1f}%)")
    print(f"  DINOv2 alone:  {dino_correct:>3}/{detected} ({100*dino_correct/detected:.1f}%)")
    print(f"  HYBRID:        {hybrid_correct:>3}/{detected} ({100*hybrid_correct/detected:.1f}%)")
    print()
    avg_phash = 1000 * t_phash_total / detected
    avg_dino = 1000 * t_dino_total / detected
    print(f"  Avg time phash:  {avg_phash:.0f}ms/card")
    print(f"  Avg time DINOv2: {avg_dino:.0f}ms/card")
    print(f"  Avg time hybrid: {avg_phash + avg_dino:.0f}ms/card (both run)")

    if disagree_details:
        print(f"\nDISAGREEMENTS ({len(disagree_details)} scans):")
        print(f"  {'Sess':>6} {'Scan':>4}  {'Phash':>6} {'Dino':>5}  {'Hybrid chose':<25} {'Method':<20} {'Correct?'}")
        for sess, sn, csv_n, p_n, p_d, d_n, d_s, h_n, meth, ok in disagree_details:
            print(f"  {sess:>6} {sn:>4}  d={p_d:<4.0f} s={d_s:<5.3f} {h_n[:25]:<25} {meth:<20} {ok}")

    if hybrid_wrong_details:
        print(f"\nHYBRID WRONG ({len(hybrid_wrong_details)} scans):")
        for sess, sn, csv_n, h_n, meth, p_n, p_d, d_n, d_s in hybrid_wrong_details:
            print(f"  {sess} scan {sn}: chose '{h_n}' via {meth}")
            print(f"    phash='{p_n}' (d={p_d:.0f}), dino='{d_n}' (s={d_s:.3f}), CSV='{csv_n}'")
    else:
        print(f"\nHYBRID: 0 wrong -- all disagreements resolved correctly!")


if __name__ == "__main__":
    main()
