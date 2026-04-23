#!/usr/bin/env python3
# prototype_foil_multisignal.py
# ---------------------------------------------------------------------------
# Runs the multi-signal foil detector (foil_detect.detect_foil) over every
# scan in session_20260415_130451 and reports:
#   - confidence distribution
#   - top-30 + bottom-10 scans by confidence
#   - known-foil reference (scan 205)
#   - any scans flagged is_foil at the default threshold
#
# Use this to tune FOIL_CONFIDENCE_THRESHOLD and the per-signal weights
# after hand-labeling the top scoring crops.
# ---------------------------------------------------------------------------

import os
import sys
import csv
import cv2

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from foil_detect import detect_foil, FOIL_CONFIDENCE_THRESHOLD
from cards import CARDS_DATA

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
CROPS_DIR = os.path.join(SESSION_DIR, "card_crops")
CSV_PATH = os.path.join(SESSION_DIR, "scans.csv")
OUT_DIR = os.path.join(SCRIPT_DIR, "foil_multisignal")
os.makedirs(OUT_DIR, exist_ok=True)


def _resolve_card_id(name, set_code, collector_number):
    """Find a card_id for a (name, set, number) triple. Returns None if
    nothing matches. Falls back to any printing of the named card that
    has a downloaded reference PNG."""
    if not name:
        return None

    name_lower = name.lower()
    set_lower = (set_code or "").lower()

    # Try exact (name, set, num) first
    for c in CARDS_DATA:
        if (c.get("name", "").lower() == name_lower
                and c.get("set", "").lower() == set_lower
                and str(c.get("collector_number", "")) == str(collector_number)):
            cid = c.get("id")
            if cid and os.path.isfile(os.path.join(
                    SCRIPT_DIR, "downloaded_cards", f"{cid}.png")):
                return cid

    # Relax to (name, set)
    for c in CARDS_DATA:
        if (c.get("name", "").lower() == name_lower
                and c.get("set", "").lower() == set_lower):
            cid = c.get("id")
            if cid and os.path.isfile(os.path.join(
                    SCRIPT_DIR, "downloaded_cards", f"{cid}.png")):
                return cid

    # Fall back to any printing by name
    for c in CARDS_DATA:
        if c.get("name", "").lower() == name_lower:
            cid = c.get("id")
            if cid and os.path.isfile(os.path.join(
                    SCRIPT_DIR, "downloaded_cards", f"{cid}.png")):
                return cid

    return None


def main():
    csv_rows = {}
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                csv_rows[int(row["scan_num"])] = row
            except (KeyError, ValueError):
                continue

    scan_files = sorted(
        f for f in os.listdir(CROPS_DIR)
        if f.startswith("card_") and f.endswith(".jpg")
    )
    print(f"Processing {len(scan_files)} card crops...")

    results = []
    skipped = 0
    no_ref = 0

    for i, fname in enumerate(scan_files):
        scan_num = int(fname[5:9])
        card = cv2.imread(os.path.join(CROPS_DIR, fname))
        if card is None:
            skipped += 1
            continue

        row = csv_rows.get(scan_num, {})
        card_id = _resolve_card_id(
            row.get("name"),
            row.get("set"),
            row.get("collector_number"),
        )

        result = detect_foil(card, card_id=card_id)
        results.append({
            "scan_num": scan_num,
            "name": row.get("name", ""),
            "set": row.get("set", ""),
            "card_id": card_id,
            "card": card,
            "result": result,
        })
        if result.get("reason") == "no_reference":
            no_ref += 1

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(scan_files)} processed")

    print(f"\nProcessed {len(results)}, skipped {skipped}, "
          f"no_reference: {no_ref}")

    # Filter to results with valid reference comparison
    usable = [r for r in results if r["result"].get("reason") == "ok"]
    print(f"Usable (reference + enough bright): {len(usable)}")

    if not usable:
        print("No usable results; aborting")
        return

    # Sort by confidence descending
    usable.sort(key=lambda r: r["result"]["confidence"], reverse=True)

    # Distribution
    import numpy as np
    confs = np.array([r["result"]["confidence"] for r in usable])
    print(f"\nConfidence distribution:")
    print(f"  min    = {confs.min():+.3f}")
    print(f"  p10    = {np.percentile(confs, 10):+.3f}")
    print(f"  median = {np.percentile(confs, 50):+.3f}")
    print(f"  mean   = {confs.mean():+.3f}")
    print(f"  p90    = {np.percentile(confs, 90):+.3f}")
    print(f"  p99    = {np.percentile(confs, 99):+.3f}")
    print(f"  max    = {confs.max():+.3f}")
    print(f"  threshold = {FOIL_CONFIDENCE_THRESHOLD}")

    flagged = [r for r in usable
               if r["result"]["confidence"] >= FOIL_CONFIDENCE_THRESHOLD]
    print(f"\nFlagged as foil at threshold {FOIL_CONFIDENCE_THRESHOLD}: "
          f"{len(flagged)}/{len(usable)}")

    # Top 30
    print(f"\nTOP 30 by confidence:")
    print(f"  {'scan':>4}  {'conf':>7}  "
          f"{'dbfr':>6}  {'dmean_s':>8}  {'dhr':>5}  "
          f"{'set':>4}  name")
    for r in usable[:30]:
        s = r["result"]["signals"]
        print(f"  {r['scan_num']:4d}  "
              f"{r['result']['confidence']:+7.3f}  "
              f"{s.get('delta_bright_frac', 0):+6.3f}  "
              f"{s.get('delta_mean_s', 0):+8.2f}  "
              f"{s.get('delta_hue_range', 0):+5.1f}  "
              f"{r['set']:>4}  {r['name']}")

    # Bottom 10
    print(f"\nBOTTOM 10 by confidence:")
    for r in usable[-10:]:
        s = r["result"]["signals"]
        print(f"  {r['scan_num']:4d}  "
              f"{r['result']['confidence']:+7.3f}  "
              f"{s.get('delta_bright_frac', 0):+6.3f}  "
              f"{s.get('delta_mean_s', 0):+8.2f}  "
              f"{s.get('delta_hue_range', 0):+5.1f}  "
              f"{r['set']:>4}  {r['name']}")

    # Known reference
    print(f"\nKNOWN REFERENCE:")
    for r in usable:
        if r["scan_num"] == 205:
            rank = usable.index(r) + 1
            c = r["result"]["confidence"]
            print(f"  scan 205 (KNOWN FOIL Zombie Infestation): "
                  f"conf={c:+.3f}, rank {rank}/{len(usable)}, "
                  f"is_foil={r['result']['is_foil']}")
            break

    # Save top-30 crops so we can visually verify
    print(f"\nSaving top-30 + bottom-10 crops to {OUT_DIR}/ ...")
    for rank, r in enumerate(usable[:30], start=1):
        name_safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                            for ch in (r["name"] or "unknown"))[:40]
        fname = (f"top{rank:02d}_scan{r['scan_num']:04d}_"
                 f"c{r['result']['confidence']:+.2f}_{name_safe}.jpg")
        cv2.imwrite(os.path.join(OUT_DIR, fname), r["card"])
    for rank, r in enumerate(usable[-10:], start=1):
        name_safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                            for ch in (r["name"] or "unknown"))[:40]
        fname = (f"bot{rank:02d}_scan{r['scan_num']:04d}_"
                 f"c{r['result']['confidence']:+.2f}_{name_safe}.jpg")
        cv2.imwrite(os.path.join(OUT_DIR, fname), r["card"])

    # Dump full CSV
    out_csv = os.path.join(OUT_DIR, "multisignal_all.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "scan_num", "name", "set",
            "confidence", "is_foil", "reason",
            "delta_bright_frac", "delta_mean_s", "delta_hue_range",
            "scan_bright_frac", "scan_mean_s",
            "ref_bright_frac", "ref_mean_s",
        ])
        for r in results:
            res = r["result"]
            sigs = res.get("signals", {}) or {}
            scan_s = sigs.get("scan") or {}
            ref_s = sigs.get("reference") or {}
            w.writerow([
                r["scan_num"], r["name"], r["set"],
                f"{res.get('confidence', 0):.4f}",
                int(bool(res.get("is_foil"))),
                res.get("reason", ""),
                f"{sigs.get('delta_bright_frac', 0):.4f}"
                    if "delta_bright_frac" in sigs else "",
                f"{sigs.get('delta_mean_s', 0):.2f}"
                    if "delta_mean_s" in sigs else "",
                f"{sigs.get('delta_hue_range', 0):.1f}"
                    if "delta_hue_range" in sigs else "",
                f"{scan_s.get('bright_frac', 0):.4f}",
                f"{scan_s.get('mean_s_bright') or 0:.2f}",
                f"{ref_s.get('bright_frac', 0):.4f}",
                f"{ref_s.get('mean_s_bright') or 0:.2f}",
            ])
    print(f"Wrote {out_csv}")


if __name__ == "__main__":
    main()
