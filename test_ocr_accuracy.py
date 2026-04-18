#!/usr/bin/env python
"""
test_ocr_accuracy.py
--------------------
Test OCR accuracy on pre-warped card crop images from scan sessions.
Compares Tesseract OCR output (card title region) against CSV ground truth.

Tests multiple preprocessing approaches:
  a) Raw grayscale
  b) Otsu threshold
  c) Adaptive threshold
  d) Sharpen + Otsu
  e) Each of the above inverted (for light-on-dark card frames)

Uses the best-of-all-strategies result for each card.
"""

import os
import sys
import csv
import re
import cv2
import numpy as np
import pytesseract
from difflib import SequenceMatcher

# Tesseract path
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Card dimensions
CARD_WIDTH = 745
CARD_HEIGHT = 1040

# Two title region definitions to test:
# Config region (from config.py TITLE_REGION): fractional (0.03, 0.01, 0.80, 0.12)
CONFIG_REGION = {
    "x1": int(0.03 * CARD_WIDTH),   # 22
    "y1": int(0.01 * CARD_HEIGHT),  # 10
    "x2": int((0.03 + 0.80) * CARD_WIDTH),   # 618
    "y2": int((0.01 + 0.12) * CARD_HEIGHT),  # 135
}

# Tight region (user-specified, just the text area)
TIGHT_REGION = {
    "x1": 30,
    "y1": 30,
    "x2": 580,
    "y2": 75,
}

SCALE_FACTOR = 4  # Upscale before OCR


# ---------------------------------------------------------------------------
# Preprocessing strategies
# ---------------------------------------------------------------------------

def preprocess_raw(gray):
    """Just the raw grayscale (upscaled)."""
    return gray


def preprocess_otsu(gray):
    """Otsu global threshold."""
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def preprocess_adaptive(gray):
    """Adaptive Gaussian threshold."""
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 31, 10
    )


def preprocess_sharpen_otsu(gray):
    """Sharpen then Otsu."""
    kernel = np.array([[-1, -1, -1],
                       [-1,  9, -1],
                       [-1, -1, -1]])
    sharpened = cv2.filter2D(gray, -1, kernel)
    _, binary = cv2.threshold(sharpened, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def preprocess_morpho(gray):
    """Otsu + morphological opening."""
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    kern = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    return cv2.morphologyEx(binary, cv2.MORPH_OPEN, kern)


STRATEGIES = [
    ("raw", preprocess_raw),
    ("otsu", preprocess_otsu),
    ("adaptive", preprocess_adaptive),
    ("sharpen_otsu", preprocess_sharpen_otsu),
    ("morpho", preprocess_morpho),
]


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------

def crop_title(card_img, region):
    """Crop the title region from a 745x1040 card image."""
    return card_img[region["y1"]:region["y2"], region["x1"]:region["x2"]]


def upscale(gray, factor):
    """Upscale grayscale image for better OCR."""
    if factor <= 1:
        return gray
    h, w = gray.shape
    return cv2.resize(gray, (w * factor, h * factor), interpolation=cv2.INTER_CUBIC)


def ocr_region(region_img, scale_factor=SCALE_FACTOR):
    """
    Try all preprocessing strategies (normal + inverted) on a title region.
    Returns (best_text, best_strategy_name).
    """
    # Convert to grayscale
    if len(region_img.shape) == 3:
        gray = cv2.cvtColor(region_img, cv2.COLOR_BGR2GRAY)
    else:
        gray = region_img.copy()

    gray = upscale(gray, scale_factor)

    best_text = ""
    best_score = -1
    best_name = ""

    for name, preprocess_fn in STRATEGIES:
        for inv_label, do_invert in [("", False), ("_inv", True)]:
            img = gray.copy()
            if do_invert:
                img = cv2.bitwise_not(img)
            processed = preprocess_fn(img)
            # PSM 7 = single line of text
            text = pytesseract.image_to_string(
                processed, config='--psm 7'
            ).strip()
            # Score by alpha character count
            score = sum(c.isalpha() for c in text)
            if score > best_score:
                best_score = score
                best_text = text
                best_name = f"{name}{inv_label}"

    return best_text, best_name


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def normalize_name(name):
    """Normalize card name for comparison."""
    if not name:
        return ""
    # Remove quotes, extra whitespace
    name = name.strip().strip('"').strip("'")
    # Lowercase
    name = name.lower()
    # Remove non-alphanumeric except spaces
    name = re.sub(r'[^a-z0-9 ]', ' ', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def check_match(ocr_text, csv_name):
    """
    Check if OCR result matches the ground truth name.
    Returns (exact_match, fuzzy_ratio, contains_match).

    For split cards like "Invasion of Innistrad // Deluge of the Dead",
    we check against the first name only.
    """
    # Handle split card names - take first part
    if '//' in csv_name:
        csv_name = csv_name.split('//')[0].strip()

    norm_ocr = normalize_name(ocr_text)
    norm_csv = normalize_name(csv_name)

    if not norm_ocr or not norm_csv:
        return False, 0.0, False

    exact = (norm_ocr == norm_csv)
    fuzzy = SequenceMatcher(None, norm_ocr, norm_csv).ratio()
    contains = (norm_csv in norm_ocr) or (norm_ocr in norm_csv)

    return exact, fuzzy, contains


# ---------------------------------------------------------------------------
# Session processing
# ---------------------------------------------------------------------------

def load_csv(csv_path):
    """Load scan CSV and return dict of scan_num -> name."""
    entries = {}
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            scan_num = int(row['scan_num'])
            name = row['name']
            entries[scan_num] = name
    return entries


def process_session(session_dir, label):
    """Process one scan session. Returns list of result dicts."""
    csv_path = os.path.join(session_dir, "scans.csv")
    crops_dir = os.path.join(session_dir, "card_crops")

    if not os.path.isdir(crops_dir):
        print(f"  [SKIP] No card_crops directory in {session_dir}")
        return []

    ground_truth = load_csv(csv_path)
    results = []

    # Sort crop files numerically
    crop_files = sorted(
        [f for f in os.listdir(crops_dir) if f.startswith("card_") and f.endswith(".jpg")],
        key=lambda f: int(re.search(r'(\d+)', f).group(1))
    )

    print(f"\n{'='*80}")
    print(f"Session: {label}")
    print(f"  Directory: {session_dir}")
    print(f"  Card crops: {len(crop_files)}")
    print(f"  CSV entries: {len(ground_truth)}")
    print(f"{'='*80}")

    header = f"{'#':>4}  {'CSV Name':<40}  {'OCR (config region)':<40}  {'Match':>5}  {'Fuzzy':>5}"
    print(f"\n--- Config Region (wide) ---")
    print(header)
    print("-" * len(header))

    config_results = []
    tight_results = []

    for crop_file in crop_files:
        scan_num = int(re.search(r'(\d+)', crop_file).group(1))
        csv_name = ground_truth.get(scan_num, "???")

        crop_path = os.path.join(crops_dir, crop_file)
        card_img = cv2.imread(crop_path)
        if card_img is None:
            print(f"  [WARN] Could not load {crop_path}")
            continue

        # Config region OCR
        title_config = crop_title(card_img, CONFIG_REGION)
        ocr_config, strat_config = ocr_region(title_config)

        # Tight region OCR
        title_tight = crop_title(card_img, TIGHT_REGION)
        ocr_tight, strat_tight = ocr_region(title_tight)

        # Check matches
        exact_c, fuzzy_c, contains_c = check_match(ocr_config, csv_name)
        exact_t, fuzzy_t, contains_t = check_match(ocr_tight, csv_name)

        match_label_c = "YES" if exact_c else ("~" if fuzzy_c >= 0.7 else "no")
        match_label_t = "YES" if exact_t else ("~" if fuzzy_t >= 0.7 else "no")

        config_results.append({
            "scan_num": scan_num,
            "csv_name": csv_name,
            "ocr_text": ocr_config,
            "strategy": strat_config,
            "exact": exact_c,
            "fuzzy": fuzzy_c,
            "contains": contains_c,
        })
        tight_results.append({
            "scan_num": scan_num,
            "csv_name": csv_name,
            "ocr_text": ocr_tight,
            "strategy": strat_tight,
            "exact": exact_t,
            "fuzzy": fuzzy_t,
            "contains": contains_t,
        })

        # Truncate for display
        csv_disp = csv_name[:38] + ".." if len(csv_name) > 40 else csv_name
        ocr_c_disp = ocr_config[:38] + ".." if len(ocr_config) > 40 else ocr_config
        print(f"{scan_num:>4}  {csv_disp:<40}  {ocr_c_disp:<40}  {match_label_c:>5}  {fuzzy_c:>5.2f}")

    # Print tight region results
    print(f"\n--- Tight Region (text-only) ---")
    print(header.replace("config region", "tight region"))
    print("-" * len(header))
    for r in tight_results:
        csv_disp = r["csv_name"][:38] + ".." if len(r["csv_name"]) > 40 else r["csv_name"]
        ocr_disp = r["ocr_text"][:38] + ".." if len(r["ocr_text"]) > 40 else r["ocr_text"]
        ml = "YES" if r["exact"] else ("~" if r["fuzzy"] >= 0.7 else "no")
        print(f"{r['scan_num']:>4}  {csv_disp:<40}  {ocr_disp:<40}  {ml:>5}  {r['fuzzy']:>5.2f}")

    return config_results, tight_results


def print_summary(label, results):
    """Print accuracy summary for a set of results."""
    total = len(results)
    if total == 0:
        print(f"  {label}: No results")
        return

    exact_matches = sum(1 for r in results if r["exact"])
    fuzzy_high = sum(1 for r in results if r["fuzzy"] >= 0.7)
    fuzzy_mid = sum(1 for r in results if 0.5 <= r["fuzzy"] < 0.7)
    contains = sum(1 for r in results if r["contains"])
    avg_fuzzy = sum(r["fuzzy"] for r in results) / total

    print(f"  {label}:")
    print(f"    Total cards:       {total}")
    print(f"    Exact matches:     {exact_matches} ({100*exact_matches/total:.1f}%)")
    print(f"    Fuzzy >= 0.7:      {fuzzy_high} ({100*fuzzy_high/total:.1f}%)")
    print(f"    Fuzzy 0.5-0.7:     {fuzzy_mid} ({100*fuzzy_mid/total:.1f}%)")
    print(f"    Contains match:    {contains} ({100*contains/total:.1f}%)")
    print(f"    Avg fuzzy ratio:   {avg_fuzzy:.3f}")

    # Show worst misses
    misses = [r for r in results if not r["exact"]]
    misses.sort(key=lambda r: r["fuzzy"])
    if misses:
        print(f"    Worst misses (lowest fuzzy):")
        for r in misses[:10]:
            print(f"      #{r['scan_num']:>3}: CSV='{r['csv_name'][:35]}' "
                  f"OCR='{r['ocr_text'][:35]}' fuzzy={r['fuzzy']:.2f} "
                  f"strat={r['strategy']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    base = r"D:\Card_Sorter\Scripts\scan_logs"

    sessions = [
        (os.path.join(base, "session_20260413_144226"), "Session 144226"),
        (os.path.join(base, "session_20260413_134000"), "Session 134000"),
    ]

    all_config = []
    all_tight = []

    for session_dir, label in sessions:
        config_res, tight_res = process_session(session_dir, label)
        all_config.extend(config_res)
        all_tight.extend(tight_res)

        print(f"\n--- Summary for {label} ---")
        print_summary("Config region (wide)", config_res)
        print_summary("Tight region (text-only)", tight_res)

    print(f"\n{'='*80}")
    print(f"OVERALL RESULTS (both sessions combined)")
    print(f"{'='*80}")
    print_summary("Config region (wide)", all_config)
    print()
    print_summary("Tight region (text-only)", all_tight)

    # Strategy frequency
    print(f"\n--- Strategy frequency (config region) ---")
    strat_counts = {}
    for r in all_config:
        s = r["strategy"]
        strat_counts[s] = strat_counts.get(s, 0) + 1
    for s, c in sorted(strat_counts.items(), key=lambda x: -x[1]):
        print(f"  {s:<20}: {c}")

    print(f"\n--- Strategy frequency (tight region) ---")
    strat_counts = {}
    for r in all_tight:
        s = r["strategy"]
        strat_counts[s] = strat_counts.get(s, 0) + 1
    for s, c in sorted(strat_counts.items(), key=lambda x: -x[1]):
        print(f"  {s:<20}: {c}")


if __name__ == "__main__":
    main()
