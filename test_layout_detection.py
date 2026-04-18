"""
test_layout_detection.py
------------------------
Quick accuracy test for detect_card_layout() in detection.py.

Samples cards by Scryfall layout from the loaded card database, loads each
card's reference image from downloaded_cards/<id>.png, runs the layout
detector, and prints a per-layout confusion matrix plus a list of
misclassifications.

Usage:
    python test_layout_detection.py                 # 50 of each
    python test_layout_detection.py --per 100       # 100 of each
    python test_layout_detection.py --per 20 --save # save failing crops
    python test_layout_detection.py --dump-ratios   # dump right_ratio to csv

The detector only returns {"normal","saga","class"}, so:
    scryfall=saga    -> expect "saga"
    scryfall=class   -> expect "class"
    scryfall=case    -> expect "class"   (same layout shape: art on one side)
    scryfall=normal  -> expect "normal"
    battle (type)    -> expect "normal"  (battle type_line, any layout)

We also include a handful of transforms/adventures as spot checks — they
should read as "normal" in our detector since the art crop is typical.
"""

import argparse
import os
import random
import sys
import cv2
import numpy as np

from cards import CARDS_DATA
from detection import detect_card_layout
from config import CARD_WIDTH, CARD_HEIGHT

IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "downloaded_cards")
FAIL_DUMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "layout_test_failures")

# Map the layout label Scryfall gives us -> what detect_card_layout should return.
EXPECTED = {
    "saga":   "saga",
    "class":  "class",
    "case":   "class",
    "normal": "normal",
    # battle, transform, adventure: we'll classify expected on the fly
}


def load_card_image(card_id):
    path = os.path.join(IMAGES_DIR, f"{card_id}.png")
    if not os.path.exists(path):
        return None
    img = cv2.imread(path)
    if img is None:
        return None
    # Normalize to canonical size so detect_card_layout's hardcoded slices
    # behave as intended. downloaded_cards PNGs are already 745x1043 but
    # resize defensively in case any scryfall fetch delivered a different size.
    h, w = img.shape[:2]
    if (w, h) != (CARD_WIDTH, CARD_HEIGHT):
        img = cv2.resize(img, (CARD_WIDTH, CARD_HEIGHT),
                         interpolation=cv2.INTER_AREA)
    return img


def compute_right_ratio(card_img):
    """Same math as detect_card_layout, but returns the raw ratio."""
    h, w = card_img.shape[:2]
    y_start = int(h * 0.10)
    y_end = int(h * 0.75)
    middle = card_img[y_start:y_end, :, :]
    hsv = cv2.cvtColor(middle, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(float)
    mid_x = w // 2
    left_mean = float(np.mean(sat[:, :mid_x]))
    right_mean = float(np.mean(sat[:, mid_x:]))
    total = left_mean + right_mean
    if total == 0:
        return 0.5, left_mean, right_mean
    return right_mean / total, left_mean, right_mean


def compute_three_strip_profile(card_img):
    """Three-strip saturation profile: left/center/right thirds.

    Uses the same y-band as detect_card_layout (10%–75% of height) so the
    numbers stay comparable to right_ratio. Returns:
        (l_mean, c_mean, r_mean,
         l_frac, c_frac, r_frac)    # normalized to sum=1.0 (0 if all black)

    Rationale: left-vs-right alone can't separate battles from classes
    (battle art sits on the left ~67% of the width, class art sits on the
    left ~45%). A center strip lets us tell them apart:
        saga:   right >> center > left
        class:  left >> center ≈ right
        battle: left ≈ center >> right
        normal: left ≈ center ≈ right
    """
    h, w = card_img.shape[:2]
    y_start = int(h * 0.10)
    y_end = int(h * 0.75)
    middle = card_img[y_start:y_end, :, :]
    hsv = cv2.cvtColor(middle, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(float)

    third = w // 3
    l_mean = float(np.mean(sat[:, :third]))
    c_mean = float(np.mean(sat[:, third:2 * third]))
    r_mean = float(np.mean(sat[:, 2 * third:]))
    total = l_mean + c_mean + r_mean
    if total == 0:
        return l_mean, c_mean, r_mean, 0.0, 0.0, 0.0
    return (l_mean, c_mean, r_mean,
            l_mean / total, c_mean / total, r_mean / total)


def pick_samples(per_bucket, seed):
    """Return a dict {bucket_name: [card_dict,...]} for each layout we test.

    Only includes cards whose reference PNG actually exists on disk (the
    download pipeline dedupes by illustration_id, so most Scryfall entries
    don't have their own file).
    """
    rng = random.Random(seed)
    # Build an O(1) set of available image ids. Skip back-face files
    # (`{real_id}__back.png`) — they're hashed alongside their front face
    # for runtime matching, but they're not independent cards and the
    # layout test cares about front-face framing only.
    try:
        available = {os.path.splitext(f)[0]
                     for f in os.listdir(IMAGES_DIR)
                     if f.endswith(".png")
                     and not os.path.splitext(f)[0].endswith("__back")}
    except FileNotFoundError:
        print(f"[ERROR] images dir not found: {IMAGES_DIR}")
        sys.exit(1)
    print(f"[pick] {len(available)} front-face images on disk")

    buckets = {
        "normal": [],
        "saga":   [],
        "class":  [],
        "case":   [],
        "battle": [],
    }
    for c in CARDS_DATA:
        if c.get("lang") != "en":
            continue
        if "paper" not in c.get("games", []):
            continue
        cid = c.get("id")
        if not cid or cid not in available:
            continue
        layout = c.get("layout", "")
        type_line = c.get("type_line", "").lower()

        # Battle bucket is driven by type_line, not layout, since most
        # battles are layout=transform and don't get imaged by the
        # current downloader.
        if "battle" in type_line:
            buckets["battle"].append(c)
            continue

        if layout in buckets:
            buckets[layout].append(c)

    print("[pick] available per bucket: " +
          ", ".join(f"{k}={len(v)}" for k, v in buckets.items()))

    out = {}
    for name, cards in buckets.items():
        rng.shuffle(cards)
        out[name] = cards[:per_bucket]
    return out


def run(per_bucket, dump_failures, seed, dump_ratios, verbose):
    samples = pick_samples(per_bucket, seed)

    # Expected detector output per test bucket
    bucket_expected = {
        "normal": "normal",
        "saga":   "saga",
        "class":  "class",
        "case":   "class",
        "battle": "battle",
    }

    # confusion[bucket][predicted] = count
    confusion = {b: {"normal": 0, "saga": 0, "class": 0,
                     "battle": 0, "missing": 0}
                 for b in samples}
    wrongs = {b: [] for b in samples}
    ratio_rows = []  # (bucket, card_name, set, pred, expected, right_ratio)
    # strip_rows[bucket] -> list of dicts with three-strip profile data
    strip_rows = {b: [] for b in samples}

    if dump_failures and not os.path.exists(FAIL_DUMP_DIR):
        os.makedirs(FAIL_DUMP_DIR, exist_ok=True)

    total = sum(len(v) for v in samples.values())
    print(f"\nTesting detect_card_layout() on {total} cards "
          f"({per_bucket} per bucket, seed={seed})")
    print(f"Image dir: {IMAGES_DIR}\n")

    for bucket, cards in samples.items():
        if not cards:
            print(f"  [{bucket}] no cards available in DB, skipping")
            continue
        expected = bucket_expected[bucket]
        for c in cards:
            cid = c["id"]
            img = load_card_image(cid)
            if img is None:
                confusion[bucket]["missing"] += 1
                if verbose:
                    print(f"  [{bucket}] MISSING image for {c.get('name')} [{cid[:8]}]")
                continue

            pred = detect_card_layout(img)
            confusion[bucket][pred] += 1

            rr, lm, rm = compute_right_ratio(img)
            ratio_rows.append((bucket, c.get("name", "?"),
                               c.get("set", "?"), pred, expected, rr))

            l3, c3, r3, lf, cf, rf = compute_three_strip_profile(img)
            strip_rows[bucket].append({
                "name": c.get("name", "?"),
                "set": c.get("set", "?"),
                "l": l3, "c": c3, "r": r3,
                "lf": lf, "cf": cf, "rf": rf,
            })

            if pred != expected:
                wrongs[bucket].append({
                    "name": c.get("name", "?"),
                    "set":  c.get("set", "?"),
                    "id":   cid,
                    "pred": pred,
                    "right_ratio": rr,
                    "left_mean": lm,
                    "right_mean": rm,
                    "lf": lf, "cf": cf, "rf": rf,
                })
                if dump_failures:
                    fname = (f"{bucket}_as_{pred}_"
                             f"{c.get('set','?')}_"
                             f"{c.get('name','?').replace(' ','_').replace('/','-')}"
                             f"_{cid[:8]}.png")
                    cv2.imwrite(os.path.join(FAIL_DUMP_DIR, fname), img)

    # ---------------- Report ----------------
    print("=" * 72)
    print("Confusion matrix (rows=actual bucket, cols=prediction)")
    print("=" * 72)
    header = (f"{'bucket':<10} {'N':>5} {'normal':>8} {'saga':>6} "
              f"{'class':>7} {'battle':>7} {'miss':>6} {'acc':>8}")
    print(header)
    print("-" * len(header))
    for bucket in ("normal", "saga", "class", "case", "battle"):
        row = confusion[bucket]
        n = sum(row.values())
        if n == 0:
            print(f"{bucket:<10} {n:>5}")
            continue
        expected = bucket_expected[bucket]
        correct = row.get(expected, 0)
        acc = correct / max(1, (n - row["missing"])) * 100.0
        print(f"{bucket:<10} {n:>5} {row['normal']:>8} {row['saga']:>6} "
              f"{row['class']:>7} {row['battle']:>7} {row['missing']:>6} "
              f"{acc:>7.1f}%")

    # Misclassifications
    print("\n" + "=" * 72)
    print("Misclassifications")
    print("=" * 72)
    any_fail = False
    for bucket, items in wrongs.items():
        if not items:
            continue
        any_fail = True
        print(f"\n[{bucket}] expected='{bucket_expected[bucket]}' — "
              f"{len(items)} wrong:")
        for w in items[:30]:
            print(f"  {w['name']:<40} [{w['set']}] -> {w['pred']:<6} "
                  f"3strip L={w['lf']:.3f} C={w['cf']:.3f} R={w['rf']:.3f}")
        if len(items) > 30:
            print(f"  ... and {len(items) - 30} more")
    if not any_fail:
        print("  (none — 100% on all buckets)")

    # Per-bucket ratio distribution (useful for tuning thresholds)
    print("\n" + "=" * 72)
    print("right_ratio stats per bucket "
          "(detector: <0.35=class, >0.65=saga, else normal)")
    print("=" * 72)
    print(f"{'bucket':<10} {'n':>5} {'min':>7} {'p10':>7} {'p50':>7} "
          f"{'p90':>7} {'max':>7} {'mean':>7}")
    for bucket in ("normal", "saga", "class", "case", "battle"):
        rs = [r[5] for r in ratio_rows if r[0] == bucket]
        if not rs:
            continue
        rs_sorted = sorted(rs)
        def pct(p): return rs_sorted[min(len(rs_sorted) - 1, int(p * len(rs_sorted)))]
        print(f"{bucket:<10} {len(rs):>5} "
              f"{min(rs):>7.3f} {pct(0.10):>7.3f} {pct(0.50):>7.3f} "
              f"{pct(0.90):>7.3f} {max(rs):>7.3f} "
              f"{sum(rs)/len(rs):>7.3f}")

    # Three-strip profile stats (left / center / right thirds of saturation)
    print("\n" + "=" * 72)
    print("three-strip saturation profile (L / C / R thirds, normalized)")
    print("=" * 72)
    print("Expected signatures:")
    print("  saga   : R >> C > L       (art on right, text on left)")
    print("  class  : L >> C ~ R       (art on left third, text fills right 2/3)")
    print("  battle : L ~ C >> R       (art on left 2/3, text on right third)")
    print("  normal : L ~ C ~ R        (art fills whole card)")
    print()
    header = f"{'bucket':<10} {'n':>5}  " \
             f"{'L_mean':>7} {'C_mean':>7} {'R_mean':>7}  " \
             f"{'lf_p50':>7} {'cf_p50':>7} {'rf_p50':>7}  " \
             f"{'lf_p10':>7} {'lf_p90':>7}  " \
             f"{'rf_p10':>7} {'rf_p90':>7}"
    print(header)
    print("-" * len(header))
    for bucket in ("normal", "saga", "class", "case", "battle"):
        rows = strip_rows.get(bucket, [])
        if not rows:
            continue
        n = len(rows)
        l_means = sorted(r["l"] for r in rows)
        c_means = sorted(r["c"] for r in rows)
        r_means = sorted(r["r"] for r in rows)
        lfs = sorted(r["lf"] for r in rows)
        cfs = sorted(r["cf"] for r in rows)
        rfs = sorted(r["rf"] for r in rows)

        def pct(vals, p):
            return vals[min(len(vals) - 1, int(p * len(vals)))]

        print(f"{bucket:<10} {n:>5}  "
              f"{sum(l_means)/n:>7.1f} {sum(c_means)/n:>7.1f} "
              f"{sum(r_means)/n:>7.1f}  "
              f"{pct(lfs,0.50):>7.3f} {pct(cfs,0.50):>7.3f} "
              f"{pct(rfs,0.50):>7.3f}  "
              f"{pct(lfs,0.10):>7.3f} {pct(lfs,0.90):>7.3f}  "
              f"{pct(rfs,0.10):>7.3f} {pct(rfs,0.90):>7.3f}")

    # Print a handful of extreme examples per bucket so we can sanity-check
    # the profiles against real card names.
    print("\n" + "=" * 72)
    print("three-strip samples (5 per bucket, sorted by left fraction)")
    print("=" * 72)
    for bucket in ("normal", "saga", "class", "case", "battle"):
        rows = strip_rows.get(bucket, [])
        if not rows:
            continue
        print(f"\n[{bucket}]")
        # Show the 5 most "left-heavy" cards in each bucket
        rows_sorted = sorted(rows, key=lambda r: -r["lf"])
        for row in rows_sorted[:5]:
            print(f"  {row['name']:<40} [{row['set']}] "
                  f"L={row['lf']:.3f} C={row['cf']:.3f} R={row['rf']:.3f}  "
                  f"(raw L={row['l']:.1f} C={row['c']:.1f} R={row['r']:.1f})")

    if dump_ratios:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "layout_test_ratios.csv")
        # Build a name->strip dict per bucket so we can join strip stats
        # onto the ratio rows without changing ratio_rows' tuple shape.
        strip_lookup = {}
        for bucket, rows in strip_rows.items():
            for row in rows:
                strip_lookup[(bucket, row["name"], row["set"])] = row
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("bucket,name,set,pred,expected,right_ratio,"
                    "l_frac,c_frac,r_frac,l_mean,c_mean,r_mean\n")
            for r in ratio_rows:
                name = str(r[1]).replace(",", " ")
                bucket = r[0]
                sset = r[2]
                s = strip_lookup.get((bucket, r[1], sset))
                if s:
                    extra = (f",{s['lf']:.4f},{s['cf']:.4f},{s['rf']:.4f},"
                             f"{s['l']:.2f},{s['c']:.2f},{s['r']:.2f}")
                else:
                    extra = ",,,,,,"
                f.write(f"{bucket},{name},{sset},{r[3]},{r[4]},{r[5]:.4f}"
                        f"{extra}\n")
        print(f"\nWrote {len(ratio_rows)} rows to {csv_path}")

    if dump_failures:
        print(f"\nFailing crops saved to: {FAIL_DUMP_DIR}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=50,
                    help="Max samples per bucket (default 50)")
    ap.add_argument("--seed", type=int, default=42,
                    help="Random seed for reproducibility (default 42)")
    ap.add_argument("--save", action="store_true",
                    help="Save misclassified card PNGs to layout_test_failures/")
    ap.add_argument("--dump-ratios", action="store_true",
                    help="Dump all right_ratios to layout_test_ratios.csv")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    run(args.per, args.save, args.seed, args.dump_ratios, args.verbose)


if __name__ == "__main__":
    main()
