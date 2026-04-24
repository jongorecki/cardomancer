# eval_set_symbol_akaze.py
# ---------------------------------------------------------------------------
# AKAZE-feature-based set-symbol matcher. Alternative to the
# TM_CCOEFF_NORMED template matcher in eval_set_symbol_matcher.py.
#
# For each scan:
#   1. Crop the frame-appropriate ROI from the scan (with pad).
#   2. Upscale 4x so AKAZE finds enough keypoints on tiny icons.
#   3. Match AKAZE descriptors against each candidate template's
#      precomputed descriptors (BF Hamming, Lowe's ratio test).
#   4. Score = number of "good" matches surviving ratio test.
#
# Filtering mirrors eval_set_symbol_matcher.py: only scans whose
# illustration_id has exactly one English paper printing (unambiguous
# ground truth).
# ---------------------------------------------------------------------------
import argparse
import csv
import glob
import os
from collections import Counter, defaultdict
from typing import List, Optional, Tuple

import cv2
import numpy as np

from cards import CARD_DATA_BY_ID, CARDS_DATA
from config import EXCLUDED_SETS
from set_symbol_roi import get_symbol_roi

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(SCRIPT_DIR, "card_data", "set_symbols", "roi_templates")
SCAN_LOGS = os.path.join(SCRIPT_DIR, "scan_logs")

# Upscale factor applied to both scan crop and template before AKAZE.
# 50x55 is too small for stable keypoints; 4x => 200x220.
UPSCALE = 4

# Lowe's ratio test threshold. Lower = stricter.
RATIO = 0.75

# Padding added to scan crop so scanned symbol isn't flush against ROI edge.
ROI_PAD = 15


def _create_akaze():
    # threshold lower than default helps on small/low-contrast icons
    return cv2.AKAZE_create(threshold=0.0005)


def _upscale(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    return cv2.resize(img, (w * UPSCALE, h * UPSCALE),
                      interpolation=cv2.INTER_CUBIC)


def load_template_features() -> dict:
    """Return {(set_code, frame): (keypoints_list, descriptors)}.
    Uses the *gray* templates (not edge) — AKAZE handles its own
    intensity gradients and doesn't want pre-thresholded input.
    """
    akaze = _create_akaze()
    out = {}
    for fn in sorted(os.listdir(TEMPLATE_DIR)):
        if not fn.endswith("_gray.png"):
            continue
        name = fn[:-len("_gray.png")]
        try:
            set_code, frame = name.rsplit("_", 1)
        except ValueError:
            continue
        img = cv2.imread(os.path.join(TEMPLATE_DIR, fn), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        up = _upscale(img)
        kps, desc = akaze.detectAndCompute(up, None)
        if desc is None or len(kps) < 4:
            continue
        out[(set_code, frame)] = (kps, desc)
    return out


def build_card_idx():
    idx = {}
    for cid, c in CARD_DATA_BY_ID.items():
        key = (c.get("set", "").lower(), str(c.get("collector_number", "")))
        idx[key] = c
    return idx


def build_illus_reprint_counts() -> dict:
    counts = defaultdict(int)
    for c in CARDS_DATA:
        iid = c.get("illustration_id")
        if not iid or c.get("lang") != "en":
            continue
        if "paper" not in (c.get("games") or []):
            continue
        set_code = (c.get("set") or "").lower()
        if set_code in EXCLUDED_SETS:
            continue
        counts[iid] += 1
    return counts


def preprocess_scan_roi(card_img: np.ndarray, roi) -> np.ndarray:
    x, y, w, h = roi
    if card_img.shape[:2] != (1040, 745):
        card_img = cv2.resize(card_img, (745, 1040))
    x0 = max(0, x - ROI_PAD)
    y0 = max(0, y - ROI_PAD)
    x1 = min(745, x + w + ROI_PAD)
    y1 = min(1040, y + h + ROI_PAD)
    crop = card_img[y0:y1, x0:x1]
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return _upscale(crop)


def score(bf, scan_desc, tmpl_desc) -> int:
    """Count good matches after Lowe's ratio test."""
    if scan_desc is None or tmpl_desc is None:
        return 0
    if len(scan_desc) < 2 or len(tmpl_desc) < 2:
        return 0
    matches = bf.knnMatch(scan_desc, tmpl_desc, k=2)
    good = 0
    for pair in matches:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < RATIO * n.distance:
            good += 1
    return good


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", choices=["all", "same-frame"],
                    default="same-frame")
    ap.add_argument("--max-scans", type=int, default=0)
    args = ap.parse_args()

    templates = load_template_features()
    print(f"[load] {len(templates)} templates with features")
    for (s, f), (kps, _) in sorted(templates.items()):
        print(f"  {s:10s} {f:6s}  n_keypoints={len(kps)}")
    template_sets = {code for (code, _) in templates.keys()}

    card_idx = build_card_idx()
    illus_counts = build_illus_reprint_counts()
    akaze = _create_akaze()
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)

    overall = Counter()
    per_set = defaultdict(Counter)
    confusion = Counter()

    n_scans = 0
    for session in sorted(glob.glob(os.path.join(SCAN_LOGS, "session_*"))):
        csv_path = os.path.join(session, "scans.csv")
        if not os.path.exists(csv_path):
            continue
        with open(csv_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("recognized", "").lower() != "true":
                    continue
                set_code = r.get("set", "").lower()
                if set_code not in template_sets:
                    continue
                card = card_idx.get((set_code, r.get("collector_number", "")))
                if not card:
                    continue
                if card.get("frame_effects") or "Basic Land" in (card.get("type_line") or ""):
                    continue
                iid = card.get("illustration_id")
                if not iid or illus_counts.get(iid, 0) != 1:
                    continue
                frame = card.get("frame", "")
                roi = get_symbol_roi(frame, None)
                if roi is None:
                    continue
                crop = os.path.join(
                    session, "card_crops",
                    f"card_{int(r['scan_num']):04d}.jpg",
                )
                if not os.path.exists(crop):
                    continue
                img = cv2.imread(crop)
                if img is None:
                    continue
                scan_roi = preprocess_scan_roi(img, roi)
                _, scan_desc = akaze.detectAndCompute(scan_roi, None)

                if args.candidates == "same-frame":
                    cand_keys = [k for k in templates.keys() if k[1] == frame]
                else:
                    cand_keys = list(templates.keys())

                scores = []
                for key in cand_keys:
                    _, tdesc = templates[key]
                    s = score(bf, scan_desc, tdesc)
                    scores.append((key, s))
                if not scores:
                    continue
                scores.sort(key=lambda x: -x[1])
                pred_set = scores[0][0][0]
                top_score = scores[0][1]
                # If top score is zero, all candidates tied at 0 — treat
                # as "no match" rather than arbitrary prediction.
                if top_score == 0:
                    overall["no_match"] += 1
                    per_set[set_code]["no_match"] += 1
                    n_scans += 1
                    if args.max_scans and n_scans >= args.max_scans:
                        break
                    continue

                ok = (pred_set == set_code)
                overall["correct" if ok else "wrong"] += 1
                per_set[set_code]["correct" if ok else "wrong"] += 1
                confusion[(set_code, pred_set)] += 1

                n_scans += 1
                if args.max_scans and n_scans >= args.max_scans:
                    break
        if args.max_scans and n_scans >= args.max_scans:
            break

    def pct(c):
        t = c["correct"] + c["wrong"] + c["no_match"]
        return (f"{c['correct']}/{t} = "
                f"{100*c['correct']/max(1,t):.1f}%  "
                f"(no_match={c['no_match']})")

    print(f"\n=== AKAZE matcher ({args.candidates} candidates) ===")
    print(f"scans: {n_scans}")
    print(f"overall: {pct(overall)}")

    print("\nPer set (top 20 by volume):")
    ranked = sorted(per_set.items(),
                    key=lambda kv: -(kv[1]["correct"] + kv[1]["wrong"] + kv[1]["no_match"]))
    for k, v in ranked[:20]:
        print(f"  {k:10s} {pct(v)}")

    print("\nTop confusions (true -> pred):")
    wrong = [(k, v) for k, v in confusion.items() if k[0] != k[1]]
    wrong.sort(key=lambda x: -x[1])
    for (t, p), c in wrong[:10]:
        print(f"  {t:10s} -> {p:10s} x{c}")


if __name__ == "__main__":
    main()
