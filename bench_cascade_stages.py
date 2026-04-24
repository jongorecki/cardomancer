# bench_cascade_stages.py
# ---------------------------------------------------------------------------
# Benchmark per-scan latency for each printing-disambiguation cascade
# stage against a sample of real card scans. Reports median/mean/p95
# in ms so we can budget the cascade.
# ---------------------------------------------------------------------------
import csv
import glob
import os
import statistics
import time
from collections import defaultdict
from typing import Callable, List

import cv2
import numpy as np

from cards import CARD_DATA_BY_ID
from set_symbol_roi import get_symbol_roi
from frame_detect import detect_frame
from list_stamp import detect_list_stamp
from frame_era_classifier import classify as classify_frame_era
from frame_template_matcher import FrameTemplateMatcher
from eval_set_symbol_matcher import (
    load_templates, preprocess_roi, match_score, build_card_idx
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCAN_LOGS = os.path.join(SCRIPT_DIR, "scan_logs")

MAX_SCANS = 80


def collect_scans(limit: int):
    card_idx = build_card_idx()
    rows = []
    for session in sorted(glob.glob(os.path.join(SCAN_LOGS, "session_*"))):
        csv_path = os.path.join(session, "scans.csv")
        if not os.path.exists(csv_path):
            continue
        with open(csv_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("recognized", "").lower() != "true":
                    continue
                card = card_idx.get(
                    ((r.get("set") or "").lower(),
                     r.get("collector_number", ""))
                )
                if not card:
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
                if img.shape[:2] != (1040, 745):
                    img = cv2.resize(img, (745, 1040))
                rows.append((img, card))
                if len(rows) >= limit:
                    return rows
    return rows


def time_fn(fn: Callable, n_warmup: int = 2) -> List[float]:
    """Run fn() once per sample and return latencies in ms."""
    latencies = []
    for _ in range(n_warmup):
        try:
            fn()
        except Exception:
            pass
    for _ in range(1):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception:
            pass
        latencies.append((time.perf_counter() - t0) * 1000.0)
    return latencies


def summarize(label: str, values: List[float]):
    if not values:
        print(f"  {label:32s}  (no samples)")
        return
    values.sort()
    n = len(values)
    p50 = statistics.median(values)
    mean = statistics.mean(values)
    p95 = values[int(0.95 * (n - 1))]
    print(f"  {label:32s}  median {p50:6.2f} ms   mean {mean:6.2f} ms   "
          f"p95 {p95:6.2f} ms   n={n}")


def main():
    print(f"[load] collecting up to {MAX_SCANS} scans...")
    samples = collect_scans(MAX_SCANS)
    print(f"[load] got {len(samples)} scans")

    print("[load] building matchers / loading templates...")
    tmpls_edge = load_templates(use_edge=True)
    tmpls_gray = load_templates(use_edge=False)
    template_sets = {code for (code, _) in tmpls_edge.keys()}
    frame_matcher, _ = FrameTemplateMatcher.from_scan_dir(
        SCAN_LOGS, CARD_DATA_BY_ID,
    )
    print(f"[load] set-symbol templates: {len(tmpls_edge)}  "
          f"frame templates: {len(frame_matcher.templates)}")

    # Per-stage latency lists
    lat_symbol_ensemble = []
    lat_frame_detect = []      # phash-based border classifier
    lat_frame_template = []    # bottom-half correlation
    lat_frame_era_cls = []     # handcrafted-feature era+border classifier
    lat_list_stamp = []

    for i, (img, card) in enumerate(samples):
        frame = card.get("frame")
        fe = card.get("frame_effects")
        set_code = (card.get("set") or "").lower()

        # ---- set-symbol ensemble ----
        if set_code in template_sets:
            roi = get_symbol_roi(frame, fe)
            if roi is not None:
                def run_symbol():
                    e_roi = preprocess_roi(img, roi, use_edge=True)
                    g_roi = preprocess_roi(img, roi, use_edge=False)
                    cand_keys = [k for k in tmpls_edge.keys() if k[1] == frame]
                    best = (-1, None)
                    for key in cand_keys:
                        se = match_score(e_roi, tmpls_edge[key])
                        sg = match_score(g_roi, tmpls_gray[key])
                        s = max(0, se) + 0.35 * max(0, sg)
                        if s > best[0]:
                            best = (s, key)
                    return best
                lat_symbol_ensemble += time_fn(run_symbol)

        # ---- frame_detect (phash, same-color candidates) ----
        # Give it the typical cascade workload: 4 candidate combos.
        cand_combos = [(frame, fe), (frame, None), ("2015", None), ("1997", None)]
        lat_frame_detect += time_fn(lambda: detect_frame(img, cand_combos))

        # ---- frame template matcher ----
        # Filter by color like the cascade does.
        from frame_template_matcher import card_color_category
        color = card_color_category(card)
        lat_frame_template += time_fn(lambda: frame_matcher.classify(img, color=color))

        # ---- handcrafted frame-era classifier (era + border) ----
        lat_frame_era_cls += time_fn(lambda: classify_frame_era(img))

        # ---- list-stamp detector ----
        lat_list_stamp += time_fn(lambda: detect_list_stamp(img))

    print()
    print("=== Per-scan latency (warm, one timed call per sample) ===")
    summarize("set-symbol ensemble (gw=0.35)", lat_symbol_ensemble)
    summarize("frame_detect (phash borders)",  lat_frame_detect)
    summarize("frame template matcher",        lat_frame_template)
    summarize("frame_era classifier (border)", lat_frame_era_cls)
    summarize("list-stamp (plst)",             lat_list_stamp)


if __name__ == "__main__":
    main()
