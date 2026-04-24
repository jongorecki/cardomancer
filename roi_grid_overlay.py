# roi_grid_overlay.py
# ---------------------------------------------------------------------------
# Draw a 20-pixel coordinate grid over selected card scans so we can
# read off the correct set-symbol ROI in x,y coordinates (on the
# canonical 745x1040 image). Also draws the CURRENT ROI in red so we
# can see the delta.
# ---------------------------------------------------------------------------
import os
import sys

import cv2

from cards import CARD_DATA_BY_ID
from set_symbol_roi import get_symbol_roi

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "tmp", "set_symbol_roi_check")

# (label, scan path, (frame, frame_effects))
TARGETS = [
    ("2015_halohopper",
     "scan_logs/session_20260422_221541/card_crops/card_0001.jpg",
     ("2015", None)),
    ("1997_7ed_lure",
     "scan_logs/session_20260422_220758/card_crops/card_0002.jpg",
     ("1997", None)),
    ("2003_placeholder",
     None, ("2003", None)),  # filled in below if we can find one
]


def find_scans(frame_wanted: str, limit: int = 6):
    """Find recognized, clean scans whose Scryfall frame matches."""
    import csv
    import glob
    scan_logs = os.path.join(SCRIPT_DIR, "scan_logs")
    idx = {}
    for cid, c in CARD_DATA_BY_ID.items():
        idx[(c.get("set", "").lower(), str(c.get("collector_number", "")))] = c

    results = []
    for session in sorted(glob.glob(os.path.join(scan_logs, "session_*"))):
        csv_path = os.path.join(session, "scans.csv")
        if not os.path.exists(csv_path):
            continue
        with open(csv_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("recognized", "").lower() != "true":
                    continue
                card = idx.get(
                    (r.get("set", "").lower(), r.get("collector_number", ""))
                )
                if not card:
                    continue
                if card.get("frame") != frame_wanted:
                    continue
                if card.get("frame_effects"):
                    continue
                if "Basic Land" in (card.get("type_line") or ""):
                    continue
                if (card.get("full_art") or
                    card.get("textless") or
                    card.get("border_color") != "black"):
                    continue
                crop = os.path.join(
                    session, "card_crops",
                    f"card_{int(r['scan_num']):04d}.jpg",
                )
                if os.path.exists(crop):
                    results.append((crop, card, r))
                    if len(results) >= limit:
                        return results
    return results


def find_first_scan(frame_wanted: str):
    r = find_scans(frame_wanted, limit=1)
    return r[0] if r else (None, None, None)


def annotate(img_path, frame, frame_effects, label, extra_info=""):
    img = cv2.imread(img_path)
    if img is None:
        print(f"[skip] cannot read {img_path}")
        return
    if img.shape[:2] != (1040, 745):
        img = cv2.resize(img, (745, 1040))

    # Grid
    overlay = img.copy()
    for x in range(0, 745, 20):
        color = (0, 255, 0) if x % 100 == 0 else (0, 180, 0)
        cv2.line(overlay, (x, 0), (x, 1040), color, 1)
    for y in range(0, 1040, 20):
        color = (0, 255, 0) if y % 100 == 0 else (0, 180, 0)
        cv2.line(overlay, (0, y), (745, y), color, 1)
    img = cv2.addWeighted(overlay, 0.35, img, 0.65, 0)

    # Axis labels every 100px
    for x in range(100, 745, 100):
        cv2.putText(img, str(x), (x - 14, 14), cv2.FONT_HERSHEY_SIMPLEX,
                    0.42, (0, 255, 255), 1, cv2.LINE_AA)
    for y in range(100, 1040, 100):
        cv2.putText(img, str(y), (2, y + 4), cv2.FONT_HERSHEY_SIMPLEX,
                    0.42, (0, 255, 255), 1, cv2.LINE_AA)

    # Current ROI in red
    roi = get_symbol_roi(frame, frame_effects)
    if roi:
        x, y, w, h = roi
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.putText(img, f"current: x={x} y={y} w={w} h={h}",
                    (10, 1030), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (0, 0, 255), 1, cv2.LINE_AA)

    cv2.putText(img, f"{label}  frame={frame}  {extra_info}",
                (10, 1012), cv2.FONT_HERSHEY_SIMPLEX,
                0.55, (255, 255, 255), 2, cv2.LINE_AA)

    out = os.path.join(OUT_DIR, f"{label}_grid.png")
    cv2.imwrite(out, img)
    print(f"[out] {out}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # 2015 frame, no effects
    p, card, r = find_first_scan("2015")
    if p:
        annotate(p, "2015", None,
                 f"2015_{card.get('set','').lower()}_{r.get('collector_number','')}",
                 extra_info=f"{card.get('name','')}")

    # 1997 7ED Lure
    p = os.path.join(SCRIPT_DIR, "scan_logs/session_20260422_220758/card_crops/card_0002.jpg")
    if os.path.exists(p):
        annotate(p, "1997", None, "1997_7ed_lure")

    # Multiple 2003-frame candidates (many may be bad rectifications)
    for i, (p, card, r) in enumerate(find_scans("2003", limit=6)):
        annotate(p, "2003", None,
                 f"2003cand_{i}_{card.get('set','').lower()}_{r.get('collector_number','')}",
                 extra_info=f"{card.get('name','')}")


if __name__ == "__main__":
    main()
