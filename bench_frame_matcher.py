# bench_frame_matcher.py
# ---------------------------------------------------------------------------
# Measure the runtime cost of the template-matching frame classifier:
#   - one-time: building templates from scan_logs
#   - per-classify: what the cascade would pay on each scan
#   - memory footprint of the templates dict
# ---------------------------------------------------------------------------

import glob
import os
import sys
import time

import cv2
import numpy as np

from cards import CARD_DATA_BY_ID
from frame_template_matcher import FrameTemplateMatcher

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCAN_LOGS = os.path.join(SCRIPT_DIR, "scan_logs")


def build_card_idx():
    idx = {}
    for cid, c in CARD_DATA_BY_ID.items():
        idx[(c.get("set", "").lower(), str(c.get("collector_number", "")))] = c
    return idx


def main():
    card_idx = build_card_idx()

    # Startup cost
    t0 = time.perf_counter()
    matcher, _ = FrameTemplateMatcher.from_scan_dir(SCAN_LOGS, card_idx)
    build_s = time.perf_counter() - t0
    print(f"[build] {len(matcher.templates)} templates built in {build_s:.2f}s")

    # Memory
    tmpl_bytes = sum(t.nbytes for t in matcher.templates.values())
    print(f"[mem]   templates dict: {tmpl_bytes/1024:.1f} KiB "
          f"({len(matcher.templates)} x {list(matcher.templates.values())[0].shape} float32)")

    # Grab a pool of real scans for timing
    paths = []
    for ses in sorted(glob.glob(os.path.join(SCAN_LOGS, "session_*")))[-3:]:
        paths.extend(sorted(glob.glob(os.path.join(ses, "card_crops", "*.jpg"))))
    paths = paths[:200]
    imgs = [cv2.imread(p) for p in paths]
    imgs = [i for i in imgs if i is not None]
    print(f"[data]  {len(imgs)} images loaded for timing")

    # Per-classify: color-filtered (the intended cascade use — ~4 templates)
    n = len(imgs)
    t0 = time.perf_counter()
    for img in imgs:
        matcher.classify(img, color="G")
    t_color = (time.perf_counter() - t0) / n * 1000

    # Per-classify: no color filter (all 27 templates)
    t0 = time.perf_counter()
    for img in imgs:
        matcher.classify(img, color=None)
    t_all = (time.perf_counter() - t0) / n * 1000

    print(f"[classify] color-filtered (~4 templates): {t_color:.2f} ms/call")
    print(f"[classify] no filter (all 27 templates) : {t_all:.2f} ms/call")

    # Breakdown: how much is image decode + resize vs correlation
    t0 = time.perf_counter()
    for img in imgs:
        if img.shape[:2] != (1040, 745):
            cv2.resize(img, (745, 1040))
    t_resize = (time.perf_counter() - t0) / n * 1000
    print(f"[breakdown] resize-only: {t_resize:.2f} ms/call")


if __name__ == "__main__":
    main()
