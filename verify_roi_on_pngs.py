# verify_roi_on_pngs.py
# ---------------------------------------------------------------------------
# Verify set-symbol ROIs against clean Scryfall card PNGs instead of
# noisy scans. Picks a few cards per frame era, downloads the official
# 745x1040 PNG, overlays the current ROI with a coordinate grid.
# ---------------------------------------------------------------------------
import io
import os
import time
from typing import Optional

import cv2
import numpy as np
import requests

from cards import CARDS_DATA
from set_symbol_roi import get_symbol_roi

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "tmp", "roi_verify_pngs")
CACHE_DIR = os.path.join(SCRIPT_DIR, "tmp", "card_png_cache")
SCRYFALL_SLEEP_S = 0.1

# How many cards per frame era to pull
N_PER_ERA = 3

# Cards per era we'd like to verify against. Pick well-known sets that
# show the symbol clearly.
TARGETS = {
    "2015": [("mom", "260"), ("woe", "5"), ("one", "83")],
    # Include Mirrodin block (wide horizontal symbol) — ROI must be wide enough.
    "2003": [("m13", "120"), ("mrd", "1"), ("dst", "1"), ("5dn", "1"), ("avr", "212")],
    # Classic 1997-era AND modern retro-frame reprints (e.g. RVR, "List" retro).
    "1997": [("7ed", "255"), ("usg", "1"), ("tor", "88"),
             ("rvr", "375"), ("dmr", "1")],
}


def get_card(set_code: str, collector_number: str) -> Optional[dict]:
    for c in CARDS_DATA:
        if (c.get("set", "").lower() == set_code.lower()
                and str(c.get("collector_number", "")) == collector_number
                and c.get("lang") == "en"):
            return c
    return None


def download_png(card: dict) -> Optional[np.ndarray]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cid = card.get("id", "unknown")
    path = os.path.join(CACHE_DIR, f"{cid}.png")
    if not os.path.exists(path):
        iu = card.get("image_uris") or {}
        url = iu.get("png")
        if not url:
            faces = card.get("card_faces") or []
            if faces:
                url = (faces[0].get("image_uris") or {}).get("png")
        if not url:
            return None
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            return None
        with open(path, "wb") as f:
            f.write(resp.content)
        time.sleep(SCRYFALL_SLEEP_S)
    return cv2.imread(path)


def overlay(img: np.ndarray, frame: str, frame_effects, label: str) -> np.ndarray:
    # Normalize to 745x1040
    if img.shape[:2] != (1040, 745):
        img = cv2.resize(img, (745, 1040))
    overlay_img = img.copy()

    # 20px grid, 100px major
    for x in range(0, 745, 20):
        c = (0, 255, 0) if x % 100 == 0 else (0, 180, 0)
        cv2.line(overlay_img, (x, 0), (x, 1040), c, 1)
    for y in range(0, 1040, 20):
        c = (0, 255, 0) if y % 100 == 0 else (0, 180, 0)
        cv2.line(overlay_img, (0, y), (745, y), c, 1)
    img = cv2.addWeighted(overlay_img, 0.3, img, 0.7, 0)

    for x in range(100, 745, 100):
        cv2.putText(img, str(x), (x - 14, 14), cv2.FONT_HERSHEY_SIMPLEX,
                    0.42, (0, 255, 255), 1, cv2.LINE_AA)
    for y in range(100, 1040, 100):
        cv2.putText(img, str(y), (2, y + 4), cv2.FONT_HERSHEY_SIMPLEX,
                    0.42, (0, 255, 255), 1, cv2.LINE_AA)

    roi = get_symbol_roi(frame, frame_effects)
    if roi:
        x, y, w, h = roi
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.putText(img, f"ROI x={x} y={y} w={w} h={h}", (10, 1030),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)
    cv2.putText(img, label, (10, 1012), cv2.FONT_HERSHEY_SIMPLEX,
                0.55, (255, 255, 255), 2, cv2.LINE_AA)
    return img


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for era, targets in TARGETS.items():
        for set_code, num in targets:
            card = get_card(set_code, num)
            if not card:
                print(f"[miss] {set_code}/{num} not in CARDS_DATA")
                continue
            img = download_png(card)
            if img is None:
                print(f"[fail] could not download {set_code}/{num}")
                continue
            frame = card.get("frame")
            fe = card.get("frame_effects")
            label = f"{era}  {set_code.upper()} #{num}  frame={frame}  {card.get('name','')}"
            annotated = overlay(img, frame, fe, label)
            out = os.path.join(OUT_DIR, f"{era}_{set_code}_{num}.png")
            cv2.imwrite(out, annotated)
            print(f"[out] {out}")


if __name__ == "__main__":
    main()
