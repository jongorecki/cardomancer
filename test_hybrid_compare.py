#!/usr/bin/env python3
"""
Compare phash vs DINOv2 ViT-B/14 error overlap.
For each scan: run both methods, see which gets it right/wrong.
Goal: determine if a hybrid approach can beat either method alone.
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
    """Fuzzy check if identified name matches CSV name."""
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


def main():
    phash_load()

    total = 0
    detected = 0

    both_right = 0
    both_wrong = 0
    phash_only = 0     # phash right, dino wrong
    dino_only = 0      # dino right, phash wrong

    phash_only_list = []
    dino_only_list = []
    both_wrong_list = []

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
        print(f"{'Scan':>4} {'P-Dist':>6} {'D-Sim':>5} {'Phash Name':<25} {'DINOv2 Name':<25} {'CSV Name':<25} {'Result'}")
        print("-" * 130)

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
                print(f"{scan_num:>4} {'---':>6} {'---':>5} {'NO_DETECT':<25} {'NO_DETECT':<25} {csv_name[:25]:<25}")
                continue

            detected += 1

            # Phash
            p_id, p_dist, p_rot, _ = phash_identify(card_img)
            p_name = get_name(p_id)
            p_correct = name_match(p_name, csv_name)

            # DINOv2
            d_id, d_sim, d_rot, _ = dino_identify(card_img)
            d_name = get_name(d_id)
            d_correct = name_match(d_name, csv_name)

            if p_correct and d_correct:
                both_right += 1
                result = "BOTH OK"
            elif p_correct and not d_correct:
                phash_only += 1
                result = "PHASH ONLY"
                phash_only_list.append((session_name[-6:], scan_num, csv_name, p_name, p_dist, d_name, d_sim))
            elif d_correct and not p_correct:
                dino_only += 1
                result = "DINO ONLY"
                dino_only_list.append((session_name[-6:], scan_num, csv_name, p_name, p_dist, d_name, d_sim))
            else:
                both_wrong += 1
                result = "BOTH WRONG"
                both_wrong_list.append((session_name[-6:], scan_num, csv_name, p_name, p_dist, d_name, d_sim))

            print(f"{scan_num:>4} {p_dist:>6.1f} {d_sim:>5.3f} {p_name[:25]:<25} {d_name[:25]:<25} {csv_name[:25]:<25} {result}")

    # Summary
    print("\n" + "=" * 130)
    print(f"TOTAL: {total} scans, {detected} detected\n")
    print(f"  Both correct:     {both_right:>3}/{detected} ({100*both_right/detected:.1f}%)")
    print(f"  Phash only right: {phash_only:>3}/{detected} ({100*phash_only/detected:.1f}%)")
    print(f"  DINOv2 only right:{dino_only:>3}/{detected} ({100*dino_only/detected:.1f}%)")
    print(f"  Both wrong:       {both_wrong:>3}/{detected} ({100*both_wrong/detected:.1f}%)")
    print()
    print(f"  Phash alone:      {both_right+phash_only:>3}/{detected} ({100*(both_right+phash_only)/detected:.1f}%)")
    print(f"  DINOv2 alone:     {both_right+dino_only:>3}/{detected} ({100*(both_right+dino_only)/detected:.1f}%)")
    print(f"  HYBRID (either):  {both_right+phash_only+dino_only:>3}/{detected} ({100*(both_right+phash_only+dino_only)/detected:.1f}%)")
    print(f"  Unreachable:      {both_wrong:>3}/{detected} ({100*both_wrong/detected:.1f}%)")

    if phash_only_list:
        print(f"\nPHASH ONLY ({len(phash_only_list)} scans) — phash right, DINOv2 wrong:")
        for s, sn, csv_n, p_n, p_d, d_n, d_s in phash_only_list:
            print(f"  {s} scan {sn:>2}: CSV='{csv_n[:25]}' phash='{p_n[:20]}' (d={p_d:.0f}) dino='{d_n[:20]}' (s={d_s:.3f})")

    if dino_only_list:
        print(f"\nDINO ONLY ({len(dino_only_list)} scans) — DINOv2 right, phash wrong:")
        for s, sn, csv_n, p_n, p_d, d_n, d_s in dino_only_list:
            print(f"  {s} scan {sn:>2}: CSV='{csv_n[:25]}' phash='{p_n[:20]}' (d={p_d:.0f}) dino='{d_n[:20]}' (s={d_s:.3f})")

    if both_wrong_list:
        print(f"\nBOTH WRONG ({len(both_wrong_list)} scans) — neither method correct:")
        for s, sn, csv_n, p_n, p_d, d_n, d_s in both_wrong_list:
            print(f"  {s} scan {sn:>2}: CSV='{csv_n[:25]}' phash='{p_n[:20]}' (d={p_d:.0f}) dino='{d_n[:20]}' (s={d_s:.3f})")


if __name__ == "__main__":
    main()
