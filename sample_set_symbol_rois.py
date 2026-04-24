# sample_set_symbol_rois.py
# ---------------------------------------------------------------------------
# Pull a diverse sample of real scans across (frame_era, rarity), crop
# each one's current set_symbol_roi, and write them out as a grid
# labeled with (set, collector#, rarity, era). Lets us eyeball whether
# the ROI actually contains the symbol and whether rarity color
# segmentation looks tractable.
#
# Output: tmp/set_symbol_sample/grid.png + per-scan crops.
# ---------------------------------------------------------------------------
import csv
import glob
import os
import random
from collections import defaultdict

import cv2
import numpy as np

from cards import CARD_DATA_BY_ID
from frame_template_matcher import card_frame_era
from set_symbol_roi import get_symbol_roi

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCAN_LOGS = os.path.join(SCRIPT_DIR, "scan_logs")
OUT_DIR = os.path.join(SCRIPT_DIR, "tmp", "set_symbol_sample")

# How many scans per (era, rarity) cell
N_PER_CELL = 4


def build_card_idx():
    idx = {}
    for cid, c in CARD_DATA_BY_ID.items():
        idx[(c.get("set", "").lower(), str(c.get("collector_number", "")))] = c
    return idx


def main():
    random.seed(0)
    os.makedirs(OUT_DIR, exist_ok=True)
    card_idx = build_card_idx()

    # Group scans by (era, rarity)
    buckets = defaultdict(list)
    for session in sorted(glob.glob(os.path.join(SCAN_LOGS, "session_*"))):
        csv_path = os.path.join(session, "scans.csv")
        if not os.path.exists(csv_path):
            continue
        with open(csv_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("recognized", "").lower() != "true":
                    continue
                setc = r.get("set", "").lower()
                num = r.get("collector_number", "")
                card = card_idx.get((setc, num))
                if not card:
                    continue
                era = card_frame_era(card)
                rarity = card.get("rarity", "")
                if era is None or not rarity:
                    continue
                crop_path = os.path.join(
                    session, "card_crops",
                    f"card_{int(r['scan_num']):04d}.jpg",
                )
                if not os.path.exists(crop_path):
                    continue
                buckets[(era, rarity)].append((crop_path, card, r))

    print("[sample] bucket sizes:")
    for k in sorted(buckets):
        print(f"  {k}: {len(buckets[k])}")

    # Sample N per cell
    samples = []
    for key, rows in sorted(buckets.items()):
        random.shuffle(rows)
        samples.append((key, rows[:N_PER_CELL]))

    # Build the grid: one row per (era, rarity) cell, columns = N crops.
    # Each ROI scaled up 3x so they're visible.
    tile_h, tile_w = 135, 210
    pad = 8
    grid_rows = []
    for (era, rarity), rows in samples:
        cells = []
        for crop_path, card, scan_row in rows:
            img = cv2.imread(crop_path)
            if img is None:
                continue
            if img.shape[:2] != (1040, 745):
                img = cv2.resize(img, (745, 1040))
            roi = get_symbol_roi(card.get("frame"), card.get("frame_effects"))
            if roi is None:
                # Draw a placeholder "no ROI" tile
                tile = np.full((tile_h, tile_w, 3), 40, dtype=np.uint8)
                cv2.putText(tile, "no ROI", (10, tile_h // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
            else:
                x, y, w, h = roi
                sub = img[y:y + h, x:x + w]
                tile = cv2.resize(sub, (tile_w, tile_h),
                                  interpolation=cv2.INTER_NEAREST)
            label = f"{card.get('set', '').upper()} {scan_row.get('collector_number', '')}"
            cv2.putText(tile, label, (4, 14), cv2.FONT_HERSHEY_SIMPLEX,
                        0.4, (255, 255, 255), 1, cv2.LINE_AA)
            # Also save the raw crop so we can zoom further later
            out_name = f"{era}_{rarity}_{card.get('set','')}_{scan_row.get('collector_number','')}.png"
            out_name = out_name.replace("/", "_").replace(" ", "_")
            cv2.imwrite(os.path.join(OUT_DIR, out_name),
                        sub if roi is not None else np.zeros((1, 1, 3), dtype=np.uint8))
            cells.append(tile)

        # Pad row to N columns
        while len(cells) < N_PER_CELL:
            cells.append(np.zeros((tile_h, tile_w, 3), dtype=np.uint8))

        # Row label
        label_w = 140
        label_img = np.full((tile_h, label_w, 3), 25, dtype=np.uint8)
        cv2.putText(label_img, f"{era}", (6, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (230, 230, 230), 1, cv2.LINE_AA)
        cv2.putText(label_img, f"{rarity}", (6, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1, cv2.LINE_AA)

        # Horizontal strip
        strip = [label_img]
        for c in cells:
            strip.append(np.full((tile_h, pad, 3), 25, dtype=np.uint8))
            strip.append(c)
        grid_rows.append(np.hstack(strip))

    # Vertical stack
    max_w = max(r.shape[1] for r in grid_rows)
    padded = []
    for r in grid_rows:
        if r.shape[1] < max_w:
            extra = np.full((r.shape[0], max_w - r.shape[1], 3), 25, dtype=np.uint8)
            r = np.hstack([r, extra])
        padded.append(r)
        padded.append(np.full((pad, max_w, 3), 25, dtype=np.uint8))

    grid = np.vstack(padded)
    out_path = os.path.join(OUT_DIR, "grid.png")
    cv2.imwrite(out_path, grid)
    print(f"[out] wrote {out_path}  ({grid.shape[1]}x{grid.shape[0]})")


if __name__ == "__main__":
    main()
