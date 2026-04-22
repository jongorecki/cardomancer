#!/usr/bin/env python3
"""Save all no-detect frames as small montage so I can label them quickly."""
import os
import sys
import glob
import cv2

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NO_DETECT_GLOB = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_*", "no_detect", "nodet_*.jpg")
OUT = os.path.join(SCRIPT_DIR, "diag_out", "LABEL_all_nodetect.png")

frames = sorted(glob.glob(NO_DETECT_GLOB))
print(f"{len(frames)} frames total")

# Resize each to 200x350 and arrange in a grid
thumbs = []
labels = []
for p in frames:
    img = cv2.imread(p)
    if img is None:
        continue
    small = cv2.resize(img, (200, 350))
    label = os.path.basename(os.path.dirname(os.path.dirname(p))) + "/" + os.path.basename(p)
    thumbs.append((label, small))

# 5 across
cols = 5
rows = (len(thumbs) + cols - 1) // cols
grid_w = cols * 200
grid_h = rows * 370  # extra 20px for label
import numpy as np
grid = np.zeros((grid_h, grid_w, 3), dtype=np.uint8) + 20

for i, (label, img) in enumerate(thumbs):
    r = i // cols
    c = i % cols
    y = r * 370
    x = c * 200
    grid[y+20:y+370, x:x+200] = img
    cv2.putText(grid, os.path.basename(label), (x + 2, y + 14),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

cv2.imwrite(OUT, grid)
print(f"Saved {OUT}  ({grid_w}x{grid_h})")
