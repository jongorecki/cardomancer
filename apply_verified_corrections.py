#!/usr/bin/env python3
# apply_verified_corrections.py
# Apply manually-verified correct identifications to both CSV and DB.
# Use ONLY for cases where the crop has been visually confirmed correct.

import csv
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import collection_db
from cards import CARD_DATA_BY_ID

SESSION_ID = 36
CSV_PATH = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451", "scans.csv")

# Visually verified: detection output was CORRECT for these scans.
# Format: (scan_num, correct_card_id)
# Card IDs looked up from CARD_DATA_BY_ID by name below.
CORRECTIONS = [
    (27, "Vanquish the Weak"),
    (140, "Thrashing Frontliner"),
    (143, "Kitesail"),
]


def find_card_by_name(name):
    """Find the most common printing of a card by name."""
    candidates = []
    for cid, info in CARD_DATA_BY_ID.items():
        if info.get('name', '').lower() == name.lower():
            candidates.append((cid, info))
    if not candidates:
        return None
    # Prefer ones without art/foil variants, just pick first
    return candidates[0]


def main():
    # Resolve names to card_ids
    resolved = []
    for scan_num, name in CORRECTIONS:
        hit = find_card_by_name(name)
        if hit is None:
            print(f"ERROR: '{name}' not found in card DB for scan {scan_num}")
            return
        cid, info = hit
        resolved.append((scan_num, name, cid, info))
        print(f"scan {scan_num:4d}: {name} -> {cid} "
              f"[{info.get('set_code','?')} #{info.get('collector_number','?')}]")

    # Update DB
    conn = collection_db.get_connection()
    try:
        for scan_num, name, cid, info in resolved:
            # Check existing row
            existing = conn.execute(
                "SELECT id, name FROM scan_history "
                "WHERE session_id = ? AND scan_num = ?",
                (SESSION_ID, scan_num)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE scan_history SET "
                    "name = ?, set_code = ?, collector_number = ?, "
                    "oracle_id = ?, illustration_id = ?, "
                    "colors = ?, cmc = ?, type_line = ?, rarity = ?, "
                    "price_usd = ?, method = ?, hash_distance = ?, "
                    "recognized = 1 "
                    "WHERE id = ?",
                    (
                        info.get('name', name),
                        info.get('set_code', ''),
                        info.get('collector_number', ''),
                        info.get('oracle_id', ''),
                        info.get('illustration_id', ''),
                        ','.join(info.get('colors', []) or []),
                        info.get('cmc', 0) or 0,
                        info.get('type_line', ''),
                        info.get('rarity', ''),
                        info.get('price_usd', 0) or 0,
                        'manual_review',
                        0.0,
                        existing['id'],
                    ),
                )
                print(f"  scan {scan_num}: DB updated (was {existing['name']!r})")
            else:
                print(f"  scan {scan_num}: no DB row to update")
        conn.commit()
    finally:
        conn.close()

    # Update CSV
    with open(CSV_PATH, 'r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    corr_map = {s: (name, cid, info) for s, name, cid, info in resolved}
    updated = 0
    for r in rows:
        try:
            s = int(r['scan_num'])
        except (KeyError, ValueError):
            continue
        if s not in corr_map:
            continue
        name, cid, info = corr_map[s]
        r['name'] = info.get('name', name)
        if 'set' in fieldnames:
            r['set'] = info.get('set_code', '') or ''
        if 'collector_number' in fieldnames:
            r['collector_number'] = info.get('collector_number', '') or ''
        if 'method' in fieldnames:
            r['method'] = 'manual_review'
        if 'hash_distance' in fieldnames:
            r['hash_distance'] = '0.00'
        if 'recognized' in fieldnames:
            r['recognized'] = 'True'
        updated += 1

    tmp = CSV_PATH + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, CSV_PATH)
    print(f"\nCSV updated: {updated} rows")


if __name__ == '__main__':
    main()
