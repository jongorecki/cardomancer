#!/usr/bin/env python3
# prototype_foil_signal1.py
# ---------------------------------------------------------------------------
# Prototype of Signal 1 (bright-pixel saturation) for foil detection.
# Runs on every scan in session_20260415_130451, reports distribution, and
# saves the highest + lowest scoring crops so we can visually verify which
# are actually foils.
# ---------------------------------------------------------------------------

import os
import sys
import csv
import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from card_detect import detect_card

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
FRAMES_DIR = os.path.join(SESSION_DIR, "scan_images")
CSV_PATH = os.path.join(SESSION_DIR, "scans.csv")
OUT_DIR = os.path.join(SCRIPT_DIR, "foil_signal1")
os.makedirs(OUT_DIR, exist_ok=True)

BRIGHT_V_THRESH = 220      # pixels with V above this are "bright"
MIN_BRIGHT_PIXELS = 500    # need this many bright pixels to trust the signal


def compute_signal1(card_img):
    """
    Signal 1: mean saturation of bright pixels.

    :param card_img: 745x1040 BGR numpy array
    :return: dict with mean_s, bright_frac, n_bright, bright_count_by_val
             or None if not enough bright pixels
    """
    hsv = cv2.cvtColor(card_img, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)

    bright_mask = v > BRIGHT_V_THRESH
    n_bright = int(bright_mask.sum())
    total = card_img.shape[0] * card_img.shape[1]

    if n_bright < MIN_BRIGHT_PIXELS:
        return {
            "mean_s": None,
            "bright_frac": n_bright / total,
            "n_bright": n_bright,
            "median_s": None,
            "p90_s": None,
        }

    s_bright = s[bright_mask]
    return {
        "mean_s": float(s_bright.mean()),
        "median_s": float(np.median(s_bright)),
        "p90_s": float(np.percentile(s_bright, 90)),
        "bright_frac": n_bright / total,
        "n_bright": n_bright,
    }


def main():
    # Load scan list with names for context
    csv_rows = {}
    with open(CSV_PATH, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            try:
                csv_rows[int(row['scan_num'])] = row.get('name', '')
            except (KeyError, ValueError):
                continue

    # Iterate all scans
    results = []  # list of dicts
    skipped = 0

    scan_files = sorted(
        f for f in os.listdir(FRAMES_DIR)
        if f.startswith("scan_") and f.endswith(".jpg")
    )
    print(f"Processing {len(scan_files)} scans...")

    for i, fname in enumerate(scan_files):
        scan_num = int(fname[5:9])
        frame = cv2.imread(os.path.join(FRAMES_DIR, fname))
        if frame is None:
            skipped += 1
            continue
        card = detect_card(frame)
        if card is None:
            skipped += 1
            continue

        sig = compute_signal1(card)
        sig["scan_num"] = scan_num
        sig["name"] = csv_rows.get(scan_num, "")
        sig["card"] = card  # hold reference for later saving
        results.append(sig)

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(scan_files)} processed")

    print(f"\nProcessed {len(results)} scans, skipped {skipped}")

    # Filter to ones with valid signal (enough bright pixels)
    valid = [r for r in results if r["mean_s"] is not None]
    no_bright = [r for r in results if r["mean_s"] is None]
    print(f"Valid signal: {len(valid)} | "
          f"too few bright pixels: {len(no_bright)}")

    if not valid:
        print("No valid signals — aborting")
        return

    # Distribution stats
    mean_s_arr = np.array([r["mean_s"] for r in valid])
    print(f"\nmean_s distribution (n={len(valid)}):")
    print(f"  min    = {mean_s_arr.min():6.2f}")
    print(f"  p10    = {np.percentile(mean_s_arr, 10):6.2f}")
    print(f"  p25    = {np.percentile(mean_s_arr, 25):6.2f}")
    print(f"  median = {np.percentile(mean_s_arr, 50):6.2f}")
    print(f"  mean   = {mean_s_arr.mean():6.2f}")
    print(f"  p75    = {np.percentile(mean_s_arr, 75):6.2f}")
    print(f"  p90    = {np.percentile(mean_s_arr, 90):6.2f}")
    print(f"  p95    = {np.percentile(mean_s_arr, 95):6.2f}")
    print(f"  p99    = {np.percentile(mean_s_arr, 99):6.2f}")
    print(f"  max    = {mean_s_arr.max():6.2f}")

    # ASCII histogram
    bins = np.arange(0, 201, 10)
    counts, _ = np.histogram(mean_s_arr, bins=bins)
    print(f"\nHistogram (bucket: count bar):")
    max_c = max(counts.max(), 1)
    for j, c in enumerate(counts):
        bar = '#' * int(40 * c / max_c)
        print(f"  {bins[j]:3d}-{bins[j+1]:3d}: {c:3d} {bar}")

    # Top 20 (highest bright-S = most foil-like)
    valid_sorted = sorted(valid, key=lambda r: r["mean_s"], reverse=True)
    print(f"\nTOP 20 (highest bright-pixel saturation — candidate foils):")
    print(f"  {'scan':>4}  {'mean_s':>7}  {'p90_s':>7}  {'br_frac':>7}  name")
    for r in valid_sorted[:20]:
        print(f"  {r['scan_num']:4d}  {r['mean_s']:7.2f}  "
              f"{r['p90_s']:7.2f}  {r['bright_frac']:7.3f}  {r['name']}")

    # Bottom 20
    print(f"\nBOTTOM 20 (lowest bright-pixel saturation — definitely non-foil):")
    print(f"  {'scan':>4}  {'mean_s':>7}  {'p90_s':>7}  {'br_frac':>7}  name")
    for r in valid_sorted[-20:]:
        print(f"  {r['scan_num']:4d}  {r['mean_s']:7.2f}  "
              f"{r['p90_s']:7.2f}  {r['bright_frac']:7.3f}  {r['name']}")

    # Known reference points
    print(f"\nKNOWN REFERENCE POINTS:")
    for ref_scan, label in [(205, "KNOWN FOIL (Zombie Infestation)")]:
        for r in valid:
            if r["scan_num"] == ref_scan:
                rank = valid_sorted.index(r) + 1
                pct = 100 * rank / len(valid_sorted)
                print(f"  scan {ref_scan} ({label}): "
                      f"mean_s={r['mean_s']:.2f}  "
                      f"p90_s={r['p90_s']:.2f}  "
                      f"rank {rank}/{len(valid_sorted)} "
                      f"(top {pct:.1f}%)")
                break

    # Save crops for top 30 and bottom 15 so we can visually verify
    print(f"\nSaving top 30 + bottom 15 crops to {OUT_DIR}/ ...")
    for rank, r in enumerate(valid_sorted[:30], start=1):
        fname = (f"top{rank:02d}_scan{r['scan_num']:04d}_"
                 f"s{r['mean_s']:05.1f}.jpg")
        cv2.imwrite(os.path.join(OUT_DIR, fname), r["card"])
    for rank, r in enumerate(valid_sorted[-15:], start=1):
        fname = (f"bot{rank:02d}_scan{r['scan_num']:04d}_"
                 f"s{r['mean_s']:05.1f}.jpg")
        cv2.imwrite(os.path.join(OUT_DIR, fname), r["card"])

    # Also save known-foil scan 205 for reference
    for r in valid:
        if r["scan_num"] == 205:
            fname = f"REFERENCE_FOIL_scan0205_s{r['mean_s']:05.1f}.jpg"
            cv2.imwrite(os.path.join(OUT_DIR, fname), r["card"])
            break

    # Dump full sorted results to CSV for analysis
    out_csv = os.path.join(OUT_DIR, "signal1_all.csv")
    with open(out_csv, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(
            ["scan_num", "name", "mean_s", "median_s", "p90_s",
             "bright_frac", "n_bright"])
        for r in valid_sorted:
            w.writerow([
                r["scan_num"], r["name"],
                f"{r['mean_s']:.2f}",
                f"{r['median_s']:.2f}",
                f"{r['p90_s']:.2f}",
                f"{r['bright_frac']:.4f}",
                r["n_bright"],
            ])
        # Also append "no bright pixels" cases
        for r in no_bright:
            w.writerow([
                r["scan_num"], r["name"],
                "", "", "",
                f"{r['bright_frac']:.4f}",
                r["n_bright"],
            ])
    print(f"Wrote full CSV: {out_csv}")


if __name__ == '__main__':
    main()
