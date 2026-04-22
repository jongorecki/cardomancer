#!/usr/bin/env python3
"""
Verify that the full detect_card() pipeline (detection + warp) produces
valid 745x1040 outputs for the 8 previously-missed real-card frames.
Save the warps so we can eyeball them.
"""

import os
import sys
import cv2

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import card_detect

REAL_FRAMES = [
    ("Elvish_Doomsayer",
     "scan_logs/session_20260421_115519/no_detect/nodet_0048_attempt1.jpg"),
    ("Supernatural_Stamina",
     "scan_logs/session_20260421_115519/no_detect/nodet_0055_attempt2.jpg"),
    ("Impulsive_Pilferer",
     "scan_logs/session_20260421_115519/no_detect/nodet_0089_attempt2.jpg"),
    ("Trove_Tracker",
     "scan_logs/session_20260421_115519/no_detect/nodet_0121_attempt1.jpg"),
    ("Prickle_Faeries",
     "scan_logs/session_20260421_134408/no_detect/nodet_0014_attempt1.jpg"),
    ("Invasion_Eldraine",
     "scan_logs/session_20260421_140152/no_detect/nodet_0008_attempt1.jpg"),
    ("Invasion_Zendikar",
     "scan_logs/session_20260421_140152/no_detect/nodet_0010_attempt1.jpg"),
    ("Talarian_Contempt",
     "scan_logs/session_20260421_140152/no_detect/nodet_0076_attempt1.jpg"),
]

OUT = os.path.join(SCRIPT_DIR, "diag_out", "warps")
os.makedirs(OUT, exist_ok=True)

for name, rel in REAL_FRAMES:
    path = os.path.join(SCRIPT_DIR, rel.replace("/", os.sep))
    frame = cv2.imread(path)
    warped = card_detect.detect_card(frame)
    if warped is None:
        print(f"[FAIL] {name}")
        continue
    h, w = warped.shape[:2]
    ok = (h == 1040 and w == 745)
    print(f"[{'OK ' if ok else 'BAD'}] {name:25s}  shape={w}x{h}")
    cv2.imwrite(os.path.join(OUT, f"{name}.png"), warped)
