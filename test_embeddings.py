#!/usr/bin/env python3
"""
Test embedding-based identification against saved scan images.
Run this AFTER build_embedding_db.py finishes.

Usage:
    python test_embeddings.py

Compares embedding matches vs phash matches vs CSV ground truth.
"""

import os
import sys
import csv
import cv2
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

SESSIONS = [
    os.path.join(SCRIPT_DIR, "scan_logs", "session_20260413_144226"),
    os.path.join(SCRIPT_DIR, "scan_logs", "session_20260413_134000"),
]


def main():
    # --- Load card name lookup ---
    print("Loading card name database...")
    from cards import CARD_DATA_BY_ID
    print(f"  {len(CARD_DATA_BY_ID)} cards")

    def get_name(cid):
        if not cid:
            return "???"
        card = CARD_DATA_BY_ID.get(cid.replace("__back", ""))
        return card.get("name", cid[:12]) if card else cid[:12]

    # --- Load embedding identifier ---
    print("Loading embedding model + DB...")
    t0 = time.time()
    from card_identify_v2 import identify_card as identify_v2, is_card_back
    print(f"  Loaded in {time.time() - t0:.1f}s")

    # --- Load detector ---
    from card_detect import detect_card

    # --- Test each session ---
    total = 0
    detected = 0
    matched = 0
    same_as_csv = 0
    high_confidence = 0  # similarity > 0.7

    all_results = []

    for session_dir in SESSIONS:
        if not os.path.exists(session_dir):
            print(f"SKIP: {session_dir}")
            continue

        session_name = os.path.basename(session_dir)
        scans_csv = os.path.join(session_dir, "scans.csv")
        scan_images = os.path.join(session_dir, "scan_images")

        csv_data = {}
        with open(scans_csv, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                csv_data[int(row["scan_num"])] = row

        print(f"\nTesting {session_name} ({len(csv_data)} scans)...")
        print(f"{'Scan':>4} {'Sim':>5} {'R':>1} {'Same':>4} "
              f"{'CSV Name':<28} {'Embedding Name':<28}")
        print("-" * 80)

        for scan_num in sorted(csv_data.keys()):
            img_path = os.path.join(scan_images, f"scan_{scan_num:04d}.jpg")
            if not os.path.exists(img_path):
                continue
            total += 1

            frame = cv2.imread(img_path)
            if frame is None:
                continue

            # Detect
            card_img = detect_card(frame)
            if card_img is None:
                csv_name = csv_data[scan_num]["name"]
                print(f"{scan_num:4d}   --- - {'---':>4} {csv_name[:26]:<28} NO_DETECT")
                all_results.append({
                    "session": session_name, "scan": scan_num,
                    "status": "NO_DETECT", "csv_name": csv_name,
                })
                continue
            detected += 1

            # Identify with embeddings
            t1 = time.time()
            card_id, sim, was_rotated, top_results = identify_v2(card_img)
            elapsed_ms = (time.time() - t1) * 1000

            csv_name = csv_data[scan_num]["name"]
            new_name = get_name(card_id) if card_id else "NO_MATCH"

            # Check name match
            csv_norm = csv_name.strip().lower()
            new_norm = new_name.strip().lower()
            is_same = (csv_norm == new_norm or
                      csv_norm in new_norm or
                      new_norm in csv_norm or
                      csv_norm.split(" // ")[0] == new_norm.split(" // ")[0])

            if card_id:
                matched += 1
            if is_same:
                same_as_csv += 1
            if sim > 0.7:
                high_confidence += 1

            rot = "R" if was_rotated else ""
            same = "YES" if is_same else "NO"
            marker = " <--" if not is_same and card_id else ""

            print(f"{scan_num:4d} {sim:5.3f} {rot:>1} {same:>4} "
                  f"{csv_name[:26]:<28} {new_name[:26]:<28}{marker}")

            all_results.append({
                "session": session_name, "scan": scan_num,
                "status": "OK" if card_id else "NO_MATCH",
                "csv_name": csv_name, "new_name": new_name,
                "sim": sim, "rotated": was_rotated, "same": is_same,
                "top3": top_results[:3] if top_results else [],
                "time_ms": elapsed_ms,
            })

    # --- Summary ---
    print(f"\n{'='*80}")
    print(f"OVERALL: {total} scans")
    print(f"  Detected:       {detected}/{total} ({100*detected/total:.1f}%)")
    print(f"  Matched:        {matched}/{total} ({100*matched/total:.1f}%)")
    print(f"  Same as CSV:    {same_as_csv}/{total} ({100*same_as_csv/total:.1f}%)")
    print(f"  High conf (>0.7): {high_confidence}/{total}")

    # Timing
    timed = [r for r in all_results if "time_ms" in r]
    if timed:
        times = [r["time_ms"] for r in timed]
        print(f"  Avg time:       {sum(times)/len(times):.0f}ms per card")

    # Show mismatches with top-3
    mismatches = [r for r in all_results
                  if r.get("status") == "OK" and not r.get("same")]
    if mismatches:
        print(f"\nNAME MISMATCHES ({len(mismatches)}):")
        print("-" * 80)
        for r in mismatches:
            print(f"  {r['session'][-6:]} scan {r['scan']}: "
                  f"CSV='{r['csv_name']}' -> '{r['new_name']}' "
                  f"(sim={r['sim']:.3f})")
            if r.get("top3"):
                for i, (cid, s) in enumerate(r["top3"][:3]):
                    n = get_name(cid)
                    gap = r["sim"] - s if i > 0 else 0
                    print(f"    #{i+1}: {n[:30]} ({s:.3f})")

    # Show low-confidence matches
    low = [r for r in all_results
           if r.get("status") == "OK" and r.get("sim", 1) < 0.55]
    if low:
        print(f"\nLOW CONFIDENCE (<0.55): {len(low)} matches")
        for r in low:
            print(f"  {r['session'][-6:]} scan {r['scan']}: "
                  f"'{r['new_name'][:30]}' sim={r['sim']:.3f}")

    print(f"\n{'='*80}")
    print("Done!")


if __name__ == "__main__":
    main()
