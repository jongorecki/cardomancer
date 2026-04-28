"""
Apply foil verdicts from the review page back to a session's scans.csv.

Usage:
    python apply_foil_verdicts.py <session_dir> <verdicts.csv>

The verdicts CSV has two columns: scan_num, verdict (foil/nonfoil/skip).
Rows with verdict='skip' are left unchanged.
Backs up scans.csv to scans_prefoil.csv before writing.
"""

import csv
import os
import shutil
import sys


def run(session_dir, verdicts_path):
    csv_path = os.path.join(session_dir, "scans.csv")

    verdicts = {}
    with open(verdicts_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["verdict"] != "skip":
                verdicts[r["scan_num"]] = r["verdict"]

    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames
        for r in reader:
            rows.append(r)

    changed = 0
    for r in rows:
        v = verdicts.get(r["scan_num"])
        if v is None:
            continue
        new_val = "1" if v == "foil" else "0"
        if r["is_foil"] != new_val:
            print(f"  #{r['scan_num']:>4} {r['name'][:40]:<40}  {r['is_foil']} -> {new_val}")
            r["is_foil"] = new_val
            changed += 1

    if changed == 0:
        print("No changes — CSV unchanged.")
        return

    backup = os.path.join(session_dir, "scans_prefoil.csv")
    shutil.copy(csv_path, backup)
    print(f"\nBacked up original to scans_prefoil.csv")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"Applied {changed} foil corrections to scans.csv")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    run(sys.argv[1], sys.argv[2])
