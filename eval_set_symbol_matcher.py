# eval_set_symbol_matcher.py
# ---------------------------------------------------------------------------
# Test the PNG-template set-symbol matcher against real scans.
#
# For each recognized scan whose set has a built template:
#   1. Crop the frame-appropriate ROI from the scan (745x1040).
#   2. Convert to grayscale + Canny edges.
#   3. For every candidate set template of the same frame, run
#      matchTemplate with a small scale/rotation grid and record peak.
#   4. Top-scoring template = predicted set.
#
# Report overall accuracy + per-set breakdown.
#
# Filtering:
#   --candidates=all     — compare against every template we've built
#                          (hardest, closest to production behavior)
#   --candidates=same-frame — only same-frame templates (easier baseline)
# ---------------------------------------------------------------------------
import argparse
import csv
import glob
import os
from collections import Counter, defaultdict
from typing import List, Tuple

import cv2
import numpy as np

from cards import CARD_DATA_BY_ID, CARDS_DATA
from config import EXCLUDED_SETS
from set_symbol_roi import get_symbol_roi

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(SCRIPT_DIR, "card_data", "set_symbols", "roi_templates")
SCAN_LOGS = os.path.join(SCRIPT_DIR, "scan_logs")

CANNY_LOW = 100
CANNY_HIGH = 200

# Small sliding-scale/rotation grid. Templates were built in the
# canonical 745x1040 frame; scans may drift slightly.
SCALES = (0.95, 1.0, 1.05)
ROTATIONS = (-4, 0, 4)


def load_templates(use_edge: bool = True) -> dict:
    """Return {(set_code, frame): template_uint8}."""
    t = {}
    suffix = "_edge.png" if use_edge else "_gray.png"
    for fn in sorted(os.listdir(TEMPLATE_DIR)):
        if not fn.endswith(suffix):
            continue
        name = fn[:-len(suffix)]
        try:
            set_code, frame = name.rsplit("_", 1)
        except ValueError:
            continue
        img = cv2.imread(os.path.join(TEMPLATE_DIR, fn), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        t[(set_code, frame)] = img
    return t


def build_card_idx():
    idx = {}
    for cid, c in CARD_DATA_BY_ID.items():
        key = (c.get("set", "").lower(), str(c.get("collector_number", "")))
        idx[key] = c
    return idx


def build_illus_reprint_counts() -> dict:
    """illustration_id -> count of English paper non-excluded printings."""
    counts = defaultdict(int)
    for c in CARDS_DATA:
        iid = c.get("illustration_id")
        if not iid:
            continue
        if c.get("lang") != "en":
            continue
        if "paper" not in (c.get("games") or []):
            continue
        set_code = (c.get("set") or "").lower()
        if set_code in EXCLUDED_SETS:
            continue
        counts[iid] += 1
    return counts


ROI_PAD = 15  # px of context around the ROI so matchTemplate can slide


def preprocess_roi(card_img: np.ndarray, roi, use_edge: bool = True) -> np.ndarray:
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
    if use_edge:
        return cv2.Canny(crop, CANNY_LOW, CANNY_HIGH)
    return crop


def template_variants(tmpl: np.ndarray):
    for scale in SCALES:
        h, w = tmpl.shape[:2]
        nh = max(8, int(round(h * scale)))
        nw = max(8, int(round(w * scale)))
        r = cv2.resize(tmpl, (nw, nh), interpolation=cv2.INTER_AREA)
        for ang in ROTATIONS:
            if ang == 0:
                yield r
                continue
            m = cv2.getRotationMatrix2D((nw / 2, nh / 2), ang, 1.0)
            rot = cv2.warpAffine(r, m, (nw, nh),
                                 flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT,
                                 borderValue=0)
            yield rot


def match_score(edge_roi: np.ndarray, tmpl: np.ndarray) -> float:
    best = -1.0
    for v in template_variants(tmpl):
        vh, vw = v.shape[:2]
        if vh >= edge_roi.shape[0] or vw >= edge_roi.shape[1]:
            continue
        res = cv2.matchTemplate(edge_roi, v, cv2.TM_CCOEFF_NORMED)
        _, mx, _, _ = cv2.minMaxLoc(res)
        if mx > best:
            best = float(mx)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", choices=["all", "same-frame"],
                    default="same-frame")
    ap.add_argument("--max-scans", type=int, default=0,
                    help="Cap scans for speed (0=all)")
    ap.add_argument("--use-gray", action="store_true",
                    help="Match on grayscale instead of edges.")
    ap.add_argument("--ensemble", action="store_true",
                    help="Combine edge and gray scores (sum). "
                         "Overrides --use-gray.")
    ap.add_argument("--gray-weight", type=float, default=0.35,
                    help="Ensemble: weight for gray score (edge weight=1). "
                         "0.35 was empirically best on 420-scan validation "
                         "(73.3%% vs 69.0%% edge-only, 65.0%% gray-only).")
    args = ap.parse_args()

    if args.ensemble:
        templates_edge = load_templates(use_edge=True)
        templates_gray = load_templates(use_edge=False)
        print(f"[load] ensemble: {len(templates_edge)} edge + "
              f"{len(templates_gray)} gray templates")
        template_sets = {code for (code, _) in templates_edge.keys()}
        templates = None
    else:
        use_edge = not args.use_gray
        templates = load_templates(use_edge=use_edge)
        print(f"[load] {len(templates)} templates")
        template_sets = {code for (code, _) in templates.keys()}

    card_idx = build_card_idx()
    illus_counts = build_illus_reprint_counts()

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
                # Only validate against scans whose illustration has exactly
                # one English paper printing — then scans.csv's set label is
                # correct by construction (no cascade bias).
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
                if args.ensemble:
                    edge_roi = preprocess_roi(img, roi, use_edge=True)
                    gray_roi = preprocess_roi(img, roi, use_edge=False)
                    if edge_roi.size == 0 or gray_roi.size == 0:
                        continue
                else:
                    edge_roi = preprocess_roi(img, roi, use_edge=use_edge)
                    if edge_roi.size == 0:
                        continue

                # Score candidates
                if args.ensemble:
                    cand_keys = [k for k in templates_edge.keys() if k[1] == frame] \
                        if args.candidates == "same-frame" \
                        else list(templates_edge.keys())
                elif args.candidates == "same-frame":
                    cand_keys = [(s, frame) for (s, f) in templates.keys() if f == frame]
                else:
                    cand_keys = list(templates.keys())

                scores = []
                for key in cand_keys:
                    if args.ensemble:
                        se = match_score(edge_roi, templates_edge[key])
                        sg = match_score(gray_roi, templates_gray[key])
                        if se < 0 and sg < 0:
                            continue
                        s = max(0.0, se) + args.gray_weight * max(0.0, sg)
                    else:
                        tmpl = templates[key]
                        s = match_score(edge_roi, tmpl)
                        if s < 0:
                            continue
                    scores.append((key, s))
                if not scores:
                    continue
                scores.sort(key=lambda x: -x[1])
                pred_set = scores[0][0][0]
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
        t = c["correct"] + c["wrong"]
        return f"{c['correct']}/{t} = {100*c['correct']/max(1,t):.1f}%"

    if args.ensemble:
        mode_label = f"ensemble(gray_w={args.gray_weight})"
    else:
        mode_label = "edge" if use_edge else "gray"
    print(f"\n=== set-symbol matcher ({args.candidates} candidates, "
          f"{mode_label}) ===")
    print(f"scans: {n_scans}")
    print(f"overall: {pct(overall)}")

    print("\nPer set (top 20 by volume):")
    ranked = sorted(per_set.items(),
                    key=lambda kv: -(kv[1]["correct"] + kv[1]["wrong"]))
    for k, v in ranked[:20]:
        print(f"  {k:10s} {pct(v)}")

    # Top confusions
    print("\nTop confusions (true -> pred):")
    wrong = [(k, v) for k, v in confusion.items() if k[0] != k[1]]
    wrong.sort(key=lambda x: -x[1])
    for (t, p), c in wrong[:10]:
        print(f"  {t:10s} -> {p:10s} x{c}")


if __name__ == "__main__":
    main()
