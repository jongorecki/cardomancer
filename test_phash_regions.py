#!/usr/bin/env python3
"""
Compare phash accuracy: 3-region (A+B+C) vs 1-region (A only, both orientations).
Tests whether the B/C regions help or hurt identification.

For each scan: detect card, hash region A in both orientations, match against DB.
Compare results against CSV ground truth AND against the 3-region result.
"""

import os
import sys
import csv
import time
import cv2
import numpy as np
from PIL import Image
import imagehash

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

from card_detect import detect_card
from card_identify import (
    _load_db, _card_ids, _db_a_p, _db_b_p, _db_c_p,
    _to_pil, _auto_levels, _hash_region, _match_vectorized,
    REGION_A, REGION_B, REGION_C, HASH_SIZE, MATCH_THRESHOLD,
    identify_card,
)
from cards import CARD_DATA_BY_ID

SESSIONS = [
    ("session_20260413_144226", 89),
    ("session_20260413_134000", 71),
]


def get_name(card_id):
    """Look up card name from ID."""
    if card_id and card_id in CARD_DATA_BY_ID:
        return CARD_DATA_BY_ID[card_id].get('name', card_id[:20])
    return card_id[:20] if card_id else "???"


def identify_region_a_only(card_img):
    """
    Identify using ONLY region A in both orientations (2 comparisons).
    Returns (card_id, distance, was_rotated, all_dists).
    """
    pil = _auto_levels(_to_pil(card_img))
    rotated = pil.rotate(180)

    # Only region A, both orientations
    hash_up = _hash_region(pil, REGION_A)
    hash_rot = _hash_region(rotated, REGION_A)

    idx_up, dist_up, dists_up = _match_vectorized(hash_up, _db_a_p)
    idx_rot, dist_rot, dists_rot = _match_vectorized(hash_rot, _db_a_p)

    if dist_up <= dist_rot:
        return _card_ids[idx_up], dist_up, False, dists_up
    else:
        return _card_ids[idx_rot], dist_rot, True, dists_rot


def main():
    _load_db()

    total = 0
    detected = 0
    match_3region = 0
    match_1region = 0
    agree = 0
    only_3_correct = 0
    only_1_correct = 0

    for session_name, expected_count in SESSIONS:
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

        print(f"\nTesting {session_name} ({len(ground_truth)} scans)...")
        print(f"{'Scan':>4} {'3-Reg':>6} {'1-Reg':>6} {'3R Name':<25} {'1R Name':<25} {'CSV Name':<25} {'Notes'}")
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
                print(f"{scan_num:>4} {'---':>6} {'---':>6} {'NO_DETECT':<25} {'NO_DETECT':<25} {csv_name[:25]:<25}")
                continue

            detected += 1

            # 3-region (current production)
            id_3r, dist_3r, rot_3r, _ = identify_card(card_img)
            name_3r = get_name(id_3r)
            correct_3r = name_3r.lower().startswith(csv_name[:15].lower()) if id_3r else False
            # More lenient match
            if not correct_3r and id_3r:
                correct_3r = csv_name.lower() in name_3r.lower() or name_3r.lower() in csv_name.lower()

            # 1-region (region A only)
            id_1r, dist_1r, rot_1r, _ = identify_region_a_only(card_img)
            name_1r = get_name(id_1r)
            correct_1r = name_1r.lower().startswith(csv_name[:15].lower()) if id_1r else False
            if not correct_1r and id_1r:
                correct_1r = csv_name.lower() in name_1r.lower() or name_1r.lower() in csv_name.lower()

            if correct_3r:
                match_3region += 1
            if correct_1r:
                match_1region += 1
            if id_3r == id_1r:
                agree += 1
            if correct_3r and not correct_1r:
                only_3_correct += 1
            if correct_1r and not correct_3r:
                only_1_correct += 1

            # Notes
            notes = ""
            if correct_3r and correct_1r:
                notes = "both OK"
            elif correct_3r and not correct_1r:
                notes = "3-REG ONLY"
            elif correct_1r and not correct_3r:
                notes = "1-REG ONLY"
            elif not correct_3r and not correct_1r:
                notes = "both WRONG"

            if id_3r != id_1r:
                notes += " DIFFER"

            print(f"{scan_num:>4} {dist_3r:>6.1f} {dist_1r:>6.1f} {name_3r[:25]:<25} {name_1r[:25]:<25} {csv_name[:25]:<25} {notes}")

    # Summary
    print("\n" + "=" * 130)
    print(f"OVERALL: {total} scans, {detected} detected")
    print(f"  3-region correct: {match_3region}/{detected} ({100*match_3region/max(1,detected):.1f}%)")
    print(f"  1-region correct: {match_1region}/{detected} ({100*match_1region/max(1,detected):.1f}%)")
    print(f"  Agree (same ID):  {agree}/{detected} ({100*agree/max(1,detected):.1f}%)")
    print(f"  Only 3-reg wins:  {only_3_correct}")
    print(f"  Only 1-reg wins:  {only_1_correct}")
    print(f"\n  3-reg threshold:  {MATCH_THRESHOLD}")


if __name__ == "__main__":
    main()
