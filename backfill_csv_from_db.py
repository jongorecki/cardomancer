#!/usr/bin/env python3
# backfill_csv_from_db.py
# Back-fills scans.csv from scan_history for rows where manual corrections
# were saved to DB but never made it to CSV (pre-fix to /correct endpoint).

import csv
import os
import sys
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import collection_db


def backfill(session_id, csv_path, dry_run=False):
    """Update CSV rows from scan_history where method = manual_review."""
    conn = collection_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT scan_num, name, set_code, collector_number, "
            "method, hash_distance, recognized "
            "FROM scan_history WHERE session_id = ? AND method = ?",
            (session_id, 'manual_review')
        ).fetchall()
    finally:
        conn.close()

    corrections = {int(r['scan_num']): dict(r) for r in rows}
    print(f"Found {len(corrections)} manual_review rows in DB for "
          f"session {session_id}")

    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}")
        return

    with open(csv_path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        csv_rows = list(reader)

    updated = 0
    changes = []
    for row in csv_rows:
        try:
            s = int(row['scan_num'])
        except (KeyError, ValueError):
            continue
        if s not in corrections:
            continue
        db_row = corrections[s]
        old_name = row.get('name', '')
        new_name = db_row['name'] or old_name
        if old_name == new_name:
            continue  # Already in sync

        changes.append((s, old_name, new_name))
        row['name'] = new_name
        if 'set' in fieldnames and db_row.get('set_code'):
            row['set'] = db_row['set_code']
        if 'collector_number' in fieldnames and db_row.get('collector_number'):
            row['collector_number'] = db_row['collector_number']
        if 'method' in fieldnames:
            row['method'] = 'manual_review'
        if 'hash_distance' in fieldnames:
            row['hash_distance'] = '0.00'
        if 'recognized' in fieldnames:
            row['recognized'] = 'True'
        updated += 1

    print(f"\nChanges ({len(changes)}):")
    for s, old, new in sorted(changes):
        print(f"  scan {s:4d}: {old!r:<45} -> {new!r}")

    if dry_run:
        print("\n[dry-run] Not writing changes")
        return

    if updated == 0:
        print("\nNothing to update")
        return

    tmp = csv_path + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)
    os.replace(tmp, csv_path)
    print(f"\nWrote {updated} updates to {csv_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--session-id', type=int, default=36)
    ap.add_argument('--csv', default=os.path.join(
        'scan_logs', 'session_20260415_130451', 'scans.csv'))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    csv_path = args.csv
    if not os.path.isabs(csv_path):
        csv_path = os.path.join(SCRIPT_DIR, csv_path)
    backfill(args.session_id, csv_path, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
