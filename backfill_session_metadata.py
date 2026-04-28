"""
Backfill frame, border_color, and frame_effects columns into an existing
scans.csv that predates those columns being written by the detection loop.

Uses the local Scryfall data (default-cards-*.json, newest wins) to look up
each row by (set, collector_number).  Writes a new CSV alongside the original
named scans_enriched.csv, then renames both so the enriched version becomes
the canonical scans.csv.

Usage:
    python backfill_session_metadata.py <session_dir>
    python backfill_session_metadata.py scan_logs/session_20260424_132954
"""

import csv
import glob
import json
import os
import shutil
import sys


def load_scryfall_index():
    """Return dict keyed by (set_code, collector_number) -> card record."""
    pattern = os.path.join(os.path.dirname(__file__), "default-cards-*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError("No default-cards-*.json found")
    newest = files[-1]
    print(f"Loading Scryfall data from {os.path.basename(newest)} …")
    with open(newest, "r", encoding="utf-8") as f:
        cards = json.load(f)
    index = {}
    for c in cards:
        key = (c.get("set", "").lower(), str(c.get("collector_number", "")))
        index[key] = c
    print(f"  {len(index):,} printings indexed")
    return index


OLD_COLS = [
    "scan_num", "timestamp", "name", "set", "all_sets",
    "collector_number", "colors", "cmc", "type_line", "rarity",
    "price_usd", "bin", "method", "hash_distance", "recognized",
    "is_foil", "foil_confidence",
]
NEW_COLS = OLD_COLS + ["frame", "border_color", "frame_effects"]


def backfill(session_dir):
    csv_path = os.path.join(session_dir, "scans.csv")
    if not os.path.exists(csv_path):
        print(f"ERROR: {csv_path} not found")
        sys.exit(1)

    index = load_scryfall_index()

    enriched_path = os.path.join(session_dir, "scans_enriched.csv")
    missing = 0
    total = 0

    with open(csv_path, "r", newline="", encoding="utf-8") as fin, \
         open(enriched_path, "w", newline="", encoding="utf-8") as fout:

        reader = csv.DictReader(fin)
        existing_cols = reader.fieldnames or []

        # If already enriched, bail out.
        if "frame" in existing_cols:
            print("CSV already has frame/border_color columns — nothing to do.")
            os.remove(enriched_path)
            return

        writer = csv.DictWriter(fout, fieldnames=NEW_COLS, extrasaction="ignore")
        writer.writeheader()

        for row in reader:
            total += 1
            set_code = row.get("set", "").lower()
            cn = row.get("collector_number", "")
            card = index.get((set_code, cn))

            if card:
                fe = card.get("frame_effects") or []
                row["frame"] = card.get("frame", "")
                row["border_color"] = card.get("border_color", "")
                row["frame_effects"] = ";".join(fe)
            else:
                row["frame"] = ""
                row["border_color"] = ""
                row["frame_effects"] = ""
                missing += 1

            writer.writerow(row)

    print(f"  {total} rows processed, {missing} not found in Scryfall index")

    # Rotate: original -> scans_original.csv, enriched -> scans.csv
    original_backup = os.path.join(session_dir, "scans_original.csv")
    shutil.move(csv_path, original_backup)
    shutil.move(enriched_path, csv_path)
    print(f"Done. Original backed up to scans_original.csv")
    print(f"      Enriched CSV written to scans.csv")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    backfill(sys.argv[1])
