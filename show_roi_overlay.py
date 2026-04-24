# show_roi_overlay.py
# ---------------------------------------------------------------------------
# Draw the current set_symbol_roi as a red rectangle on a sample scan
# per frame era, so we can eyeball whether the box is actually on the
# set symbol.
# ---------------------------------------------------------------------------
import csv
import glob
import os
import random

import cv2

from cards import CARD_DATA_BY_ID
from frame_template_matcher import card_frame_era
from set_symbol_roi import get_symbol_roi

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCAN_LOGS = os.path.join(SCRIPT_DIR, "scan_logs")
OUT_DIR = os.path.join(SCRIPT_DIR, "tmp", "set_symbol_roi_check")


def build_card_idx():
    idx = {}
    for cid, c in CARD_DATA_BY_ID.items():
        idx[(c.get("set", "").lower(), str(c.get("collector_number", "")))] = c
    return idx


def main():
    random.seed(0)
    os.makedirs(OUT_DIR, exist_ok=True)
    card_idx = build_card_idx()

    want_eras = {"2003", "2015", "retro"}
    N_PER_ERA = 4
    picked = {e: [] for e in want_eras}

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
                if era not in want_eras or len(picked[era]) >= N_PER_ERA:
                    continue
                frame = card.get("frame")
                # Skip 1993 (retro w/ no symbol); pick retro=1997 only
                if era == "retro" and frame != "1997":
                    continue
                crop_path = os.path.join(
                    session, "card_crops",
                    f"card_{int(r['scan_num']):04d}.jpg",
                )
                if not os.path.exists(crop_path):
                    continue
                roi = get_symbol_roi(frame, card.get("frame_effects"))
                if roi is None:
                    continue
                picked[era].append((crop_path, card, r, roi, frame))
        if all(len(v) >= N_PER_ERA for v in picked.values()):
            break

    for era, items in picked.items():
        for i, (path, card, r, roi, frame) in enumerate(items):
            img = cv2.imread(path)
            if img.shape[:2] != (1040, 745):
                img = cv2.resize(img, (745, 1040))
            x, y, w, h = roi
            cv2.rectangle(img, (x, y), (x + w, y + h), (0, 0, 255), 3)
            label = f"{era} (frame={frame})  {card.get('set','').upper()} #{r.get('collector_number','')}"
            cv2.putText(img, label, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 0, 255), 2, cv2.LINE_AA)
            cv2.putText(img, f"ROI x={x} y={y} w={w} h={h}", (12, 58),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 1, cv2.LINE_AA)
            out_path = os.path.join(OUT_DIR, f"{era}_{i}_overlay.png")
            cv2.imwrite(out_path, img)
            print(f"[out] {era}[{i}]: {out_path}")


if __name__ == "__main__":
    main()
