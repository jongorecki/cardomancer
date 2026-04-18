#!/usr/bin/env python3
"""
Full pipeline test: detect_card() + identify_card() on all raw scan frames.
Tests multiple sessions and compares against CSV results.
"""
import os
import sys
import csv
import cv2

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from card_detect import detect_card
from card_identify import identify_card

SESSIONS = [
    os.path.join(SCRIPT_DIR, "scan_logs", "session_20260413_144226"),
    os.path.join(SCRIPT_DIR, "scan_logs", "session_20260413_134000"),
]


def load_card_names():
    try:
        from cards import CARD_DATA_BY_ID
        return CARD_DATA_BY_ID
    except Exception as e:
        print(f"WARNING: Could not load card data: {e}")
        return {}


def get_name(card_id, card_data):
    if not card_id:
        return "???"
    base_id = card_id.replace("__back", "")
    card = card_data.get(base_id)
    if card:
        name = card.get('name', '???')
        if '__back' in card_id:
            name += " (back)"
        return name
    return f"[{card_id[:8]}...]"


def test_session(session_dir, card_data):
    """Test one session, return list of result dicts."""
    scans_csv = os.path.join(session_dir, "scans.csv")
    scan_images = os.path.join(session_dir, "scan_images")
    session_name = os.path.basename(session_dir)

    csv_data = {}
    with open(scans_csv, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            csv_data[int(row['scan_num'])] = row

    results = []
    for scan_num in sorted(csv_data.keys()):
        img_path = os.path.join(scan_images, f"scan_{scan_num:04d}.jpg")
        if not os.path.exists(img_path):
            continue

        frame = cv2.imread(img_path)
        if frame is None:
            continue

        csv_name = csv_data[scan_num]['name']
        old_dist = float(csv_data[scan_num]['hash_distance'])

        card_img = detect_card(frame)
        if card_img is None:
            results.append({
                'session': session_name, 'scan': scan_num,
                'status': 'NO_DETECT', 'csv_name': csv_name,
                'new_name': '---', 'old_dist': old_dist,
                'new_dist': 0, 'rotated': False, 'same': False
            })
            continue

        card_id, dist, was_rotated, all_results_list = identify_card(card_img)
        new_name = get_name(card_id, card_data) if card_id else "NO_MATCH"

        is_same = False
        if card_id:
            csv_norm = csv_name.strip().lower()
            new_norm = new_name.strip().lower()
            is_same = (csv_norm == new_norm or
                      csv_norm in new_norm or
                      new_norm in csv_norm or
                      csv_norm.split(' // ')[0] == new_norm.split(' // ')[0])

        top3 = []
        if all_results_list:
            for cid, d in all_results_list[:3]:
                top3.append((get_name(cid, card_data), d))

        results.append({
            'session': session_name, 'scan': scan_num,
            'status': 'OK' if card_id else 'NO_MATCH',
            'csv_name': csv_name, 'new_name': new_name,
            'old_dist': old_dist, 'new_dist': dist,
            'rotated': was_rotated, 'same': is_same,
            'top3': top3
        })

    return results


def main():
    print("Loading card name database...")
    card_data = load_card_names()
    print(f"  {len(card_data)} cards loaded\n")

    all_results = []
    for session_dir in SESSIONS:
        if not os.path.exists(session_dir):
            print(f"SKIP: {session_dir}")
            continue
        name = os.path.basename(session_dir)
        print(f"Testing {name}...")
        results = test_session(session_dir, card_data)
        all_results.extend(results)
        detected = sum(1 for r in results if r['status'] != 'NO_DETECT')
        same = sum(1 for r in results if r['same'])
        print(f"  {detected}/{len(results)} detected, {same}/{len(results)} same name\n")

    # Overall summary
    total = len(all_results)
    detected = sum(1 for r in all_results if r['status'] != 'NO_DETECT')
    matched = sum(1 for r in all_results if r['status'] == 'OK')
    same = sum(1 for r in all_results if r['same'])
    diff = sum(1 for r in all_results if r['status'] == 'OK' and not r['same'])
    no_det = sum(1 for r in all_results if r['status'] == 'NO_DETECT')

    print(f"{'='*100}")
    print(f"OVERALL: {total} scans across {len(SESSIONS)} sessions")
    print(f"  Detected:       {detected}/{total} ({100*detected/total:.1f}%)")
    print(f"  Matched:        {matched}/{total} ({100*matched/total:.1f}%)")
    print(f"  Same name:      {same}/{total} ({100*same/total:.1f}%)")
    print(f"  Different name: {diff}/{total}")
    print(f"  No detect:      {no_det}/{total}")
    print(f"{'='*100}\n")

    # Print all name mismatches
    mismatches = [r for r in all_results if r['status'] == 'OK' and not r['same']]
    if mismatches:
        print(f"NAME MISMATCHES ({len(mismatches)}):")
        print(f"{'Session':<20} {'Scan':>4} {'Old':>6} {'New':>6} {'CSV Name':<28} {'New Name':<28}")
        print("-" * 100)
        for r in mismatches:
            s = r['session'][-6:]
            print(f"  ...{s:<16} {r['scan']:4d} {r['old_dist']:6.1f} {r['new_dist']:6.1f} "
                  f"{r['csv_name'][:26]:<28} {r['new_name'][:26]:<28}")

    # High-distance matches
    high = [r for r in all_results if r.get('new_dist', 0) > 85 and r['status'] == 'OK']
    if high:
        print(f"\nHIGH DISTANCE (>85): {len(high)} matches")
        for r in high:
            print(f"  ...{r['session'][-6:]} scan {r['scan']}: "
                  f"'{r['new_name'][:30]}' dist={r['new_dist']:.1f}")
            if r.get('top3'):
                for i, (n, d) in enumerate(r['top3'][:3]):
                    gap = d - r['new_dist'] if i > 0 else 0
                    print(f"    #{i+1}: {n[:30]} ({d:.1f}, gap={gap:+.1f})")

    # Regressions: old was confident (<70), new is different and higher
    regressions = [r for r in all_results
                   if r['status'] == 'OK' and not r['same']
                   and r['old_dist'] < 70 and r['new_dist'] > r['old_dist']]
    if regressions:
        print(f"\nREGRESSIONS ({len(regressions)}) — old was confident, new is different+worse:")
        for r in regressions:
            print(f"  ...{r['session'][-6:]} scan {r['scan']}: "
                  f"'{r['csv_name'][:25]}'({r['old_dist']:.1f}) -> "
                  f"'{r['new_name'][:25]}'({r['new_dist']:.1f})")


if __name__ == "__main__":
    main()
