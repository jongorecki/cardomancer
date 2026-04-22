#!/usr/bin/env python3
"""Show which specific frames pass/fail at the best single-pass config."""
import os, sys, glob
import cv2
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import card_detect

REAL_CARD_FRAMES = {
    "scan_logs/session_20260421_134408/no_detect/nodet_0014_attempt1.jpg",
    "scan_logs/session_20260421_140152/no_detect/nodet_0008_attempt1.jpg",
    "scan_logs/session_20260421_140152/no_detect/nodet_0010_attempt1.jpg",
    "scan_logs/session_20260421_115519/no_detect/nodet_0121_attempt1.jpg",
    "scan_logs/session_20260421_140152/no_detect/nodet_0076_attempt1.jpg",
}

NO_DETECT_GLOB = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_*", "no_detect", "nodet_*.jpg")

# Import the detect function from our sweep
import importlib.util
spec = importlib.util.spec_from_file_location(
    "diag_test_full", os.path.join(SCRIPT_DIR, "diag_test_full.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

if card_detect._staging_roi_area and card_detect._staging_roi_area > 0:
    min_area = int(card_detect._staging_roi_area * 0.28)
    max_area = int(card_detect._staging_roi_area * 0.60)
else:
    min_area, max_area = 80000, 600000

configs = [
    ("50/150 5x5 i3 (CURRENT)", 50, 150, 5, 3),
    ("30/90 3x3 i1", 30, 90, 3, 1),
    ("30/90 3x3 i2", 30, 90, 3, 2),
    ("30/90 3x3 i3", 30, 90, 3, 3),
    ("40/120 3x3 i1", 40, 120, 3, 1),
    ("40/120 5x5 i1", 40, 120, 5, 1),
]

frames = sorted(glob.glob(NO_DETECT_GLOB))
print(f"{'frame':60s} " + " ".join(f"{n:16s}" for n, *_ in configs))
for p in frames:
    rel = os.path.relpath(p, SCRIPT_DIR).replace("\\", "/")
    frame = cv2.imread(p)
    row = [os.path.basename(rel)]
    label = "REAL" if rel in REAL_CARD_FRAMES else ""
    row.append(label.ljust(5))
    for _, lo, hi, ks, mi in configs:
        ok = m.detect(frame, lo, hi, ks, mi, min_area, max_area)
        row.append(" Y " if ok else " . ")
    print(f"{rel[-55:]:55s}  {label:5s}  " + "  ".join(
        " Y" if m.detect(frame, lo, hi, ks, mi, min_area, max_area) else " ."
        for _, lo, hi, ks, mi in configs))
