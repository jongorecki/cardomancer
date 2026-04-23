# build_png_roi_templates.py
# ---------------------------------------------------------------------------
# Build set-symbol templates from clean Scryfall card PNGs.
#
# For each (set, frame) combo present in scan_logs, pick up to N clean
# cards, download their PNGs, crop the frame-appropriate ROI, and
# average the grayscale crops into a reference template.
#
# Output:
#   card_data/set_symbols/roi_templates/{set}_{frame}_gray.png
#   card_data/set_symbols/roi_templates/{set}_{frame}_edge.png
#
# Reruns skip sets whose templates already exist.
# ---------------------------------------------------------------------------
import argparse
import csv
import glob
import os
import time
from collections import defaultdict
from typing import List, Optional, Tuple

import cv2
import numpy as np
import requests

from cards import CARDS_DATA, CARD_DATA_BY_ID
from set_symbol_roi import get_symbol_roi

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "card_data", "set_symbols", "roi_templates")
CACHE_DIR = os.path.join(SCRIPT_DIR, "tmp", "card_png_cache")
SCAN_LOGS = os.path.join(SCRIPT_DIR, "scan_logs")
SCRYFALL_SLEEP_S = 0.1

N_CARDS_PER_SET = 8  # how many cards we try to pool per (set, frame)

# Sets that reprint other sets' cards with their *original* set symbols.
# Averaging those crops produces a mush-template that loosely matches
# anything, so exclude them from the template library. (cmr, jmp, mat
# do have their own set symbols and stay in.)
EXCLUDE_SETS_FROM_TEMPLATES = frozenset({
    "plst",  # The List
})

CANNY_LOW = 100
CANNY_HIGH = 200


def sets_in_scan_logs() -> set:
    s = set()
    for ses in sorted(glob.glob(os.path.join(SCAN_LOGS, "session_*"))):
        p = os.path.join(ses, "scans.csv")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("recognized", "").lower() == "true":
                    code = r.get("set", "").lower()
                    if code:
                        s.add(code)
    return s


def clean_cards_by_set_frame() -> dict:
    """Return {(set_code, frame): [card_dict, ...]} for cards that are
    clean enough to use as template material: black border, no weird
    frame effects, no full-art, English, paper, non-basic-land."""
    groups = defaultdict(list)
    for c in CARDS_DATA:
        if c.get("lang") != "en":
            continue
        if "paper" not in (c.get("games") or []):
            continue
        if c.get("border_color") != "black":
            continue
        if c.get("frame_effects"):
            continue
        if c.get("full_art") or c.get("textless"):
            continue
        if "Basic Land" in (c.get("type_line") or ""):
            continue
        frame = c.get("frame")
        if frame not in ("1993", "1997", "2003", "2015", "future"):
            continue
        set_code = (c.get("set") or "").lower()
        if not set_code:
            continue
        if set_code in EXCLUDE_SETS_FROM_TEMPLATES:
            continue
        groups[(set_code, frame)].append(c)
    return groups


def png_url(card: dict) -> Optional[str]:
    iu = card.get("image_uris") or {}
    if iu.get("png"):
        return iu["png"]
    faces = card.get("card_faces") or []
    if faces:
        return (faces[0].get("image_uris") or {}).get("png")
    return None


def load_png(card: dict) -> Optional[np.ndarray]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cid = card.get("id", "")
    path = os.path.join(CACHE_DIR, f"{cid}.png")
    if not os.path.exists(path):
        url = png_url(card)
        if not url:
            return None
        try:
            resp = requests.get(url, timeout=30)
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        with open(path, "wb") as f:
            f.write(resp.content)
        time.sleep(SCRYFALL_SLEEP_S)
    return cv2.imread(path)


def build_template(
    cards: List[dict],
    frame: str,
    n_cards: int = N_CARDS_PER_SET,
) -> Optional[Tuple[np.ndarray, int]]:
    """Average grayscale crops of the symbol ROI across up to n_cards.
    Returns (template_f32, n_used)."""
    roi = get_symbol_roi(frame, None)
    if roi is None:
        return None
    x, y, w, h = roi

    acc = None
    used = 0
    for c in cards[: n_cards * 3]:  # over-sample to absorb download failures
        img = load_png(c)
        if img is None:
            continue
        if img.shape[:2] != (1040, 745):
            img = cv2.resize(img, (745, 1040))
        crop = img[y:y + h, x:x + w]
        if crop.ndim == 3:
            crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        f = crop.astype(np.float32)
        acc = f if acc is None else acc + f
        used += 1
        if used >= n_cards:
            break

    if acc is None or used == 0:
        return None
    return (acc / used), used


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0,
                        help="Only build templates for the top N sets by scan count.")
    parser.add_argument("--only-sets", type=str, default="",
                        help="Comma-separated list of set codes to restrict to.")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild even if template already exists.")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    # Rank sets by scan count so we build the useful ones first
    scan_counts = defaultdict(int)
    for ses in sorted(glob.glob(os.path.join(SCAN_LOGS, "session_*"))):
        p = os.path.join(ses, "scans.csv")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("recognized", "").lower() == "true":
                    scan_counts[(r.get("set", "").lower(), "")] += 1
    ranked_sets = [k[0] for k, _ in sorted(
        scan_counts.items(), key=lambda kv: -kv[1]
    )]
    if args.only_sets:
        want = {s.strip().lower() for s in args.only_sets.split(",") if s.strip()}
        ranked_sets = [s for s in ranked_sets if s in want]
    if args.limit > 0:
        ranked_sets = ranked_sets[:args.limit]

    groups = clean_cards_by_set_frame()
    total = 0
    built = 0
    skipped = 0
    failed = []

    for set_code in ranked_sets:
        # Build a template for every frame this set has clean cards in
        for frame in ("1993", "1997", "2003", "2015", "future"):
            cards = groups.get((set_code, frame), [])
            if not cards:
                continue
            total += 1
            out_gray = os.path.join(OUT_DIR, f"{set_code}_{frame}_gray.png")
            out_edge = os.path.join(OUT_DIR, f"{set_code}_{frame}_edge.png")
            if os.path.exists(out_gray) and os.path.exists(out_edge) and not args.force:
                skipped += 1
                continue

            result = build_template(cards, frame)
            if result is None:
                failed.append((set_code, frame))
                continue
            tmpl, n_used = result
            gray = np.clip(tmpl, 0, 255).astype(np.uint8)
            cv2.imwrite(out_gray, gray)
            edges = cv2.Canny(gray, CANNY_LOW, CANNY_HIGH)
            cv2.imwrite(out_edge, edges)
            built += 1
            print(f"[ok] {set_code}/{frame}  n={n_used}  -> {os.path.basename(out_gray)}")

    print()
    print(f"[done] considered {total}, built {built}, skipped {skipped}, "
          f"failed {len(failed)}")
    if failed:
        print(f"[done] failures: {failed[:10]}")


if __name__ == "__main__":
    main()
