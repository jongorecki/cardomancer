# fetch_set_symbols.py
# ---------------------------------------------------------------------------
# Phase 4 of the printing-disambiguation pipeline: build the set-icon
# asset library.
#
# For every set in Scryfall's /sets catalog:
#   1. Download icon_svg_uri -> card_data/set_symbols/svg/{set}.svg
#   2. Rasterize at 32, 48, 64 px tall (monochrome black-on-transparent,
#      rarity tint discarded) -> card_data/set_symbols/png/{set}_{size}.png
#   3. Run Canny on each raster -> card_data/set_symbols/edge/{set}_{size}.png
#
# Also:
#   - Logs pairwise phash distance between the 48 px rasters for every
#     pair of sets. Confusable pairs (distance <= CONFUSABLE_HASH_DIST)
#     are written to card_data/set_symbols/confusable_pairs.json so
#     Phase 6 can warn when they show up as candidates together.
#
# Rerun-safe: files already on disk are skipped. To force a refresh,
# delete the cache dir (or the specific {set}.svg).
#
# Usage:
#   python fetch_set_symbols.py                  # incremental
#   python fetch_set_symbols.py --force          # redownload + re-render all
#   python fetch_set_symbols.py --rasterize-only # skip network; redo PNG/edge
# ---------------------------------------------------------------------------

import argparse
import io
import json
import os
import sys
import time
from typing import Iterable, List, Optional, Tuple

import cv2
import numpy as np
import requests
from PIL import Image
import imagehash

from reportlab.graphics import renderPM
from svglib.svglib import svg2rlg


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SET_SYMBOLS_DIR = os.path.join(SCRIPT_DIR, "card_data", "set_symbols")
SVG_DIR = os.path.join(SET_SYMBOLS_DIR, "svg")
PNG_DIR = os.path.join(SET_SYMBOLS_DIR, "png")
EDGE_DIR = os.path.join(SET_SYMBOLS_DIR, "edge")
CONFUSABLE_PATH = os.path.join(SET_SYMBOLS_DIR, "confusable_pairs.json")

SCRYFALL_SETS_URL = "https://api.scryfall.com/sets"
SCRYFALL_RATE_LIMIT_S = 0.1  # 100 ms between requests per Scryfall API guidelines

# Rasters at multiple sizes so Phase 6 can pick the closest to the ROI crop
RASTER_SIZES = (32, 48, 64)

# Canny thresholds tuned for rasterized icon art — icons are already high-
# contrast after monochrome normalization; low thresholds would produce
# internal noise.
CANNY_LOW = 100
CANNY_HIGH = 200

# Pairwise hash distance <= this -> flagged confusable (on 64-bit phash,
# so 0..64). Tuned conservatively; Phase 6 will narrow by frame era anyway.
CONFUSABLE_HASH_DIST = 8


def ensure_dirs() -> None:
    for d in (SVG_DIR, PNG_DIR, EDGE_DIR):
        os.makedirs(d, exist_ok=True)


def fetch_sets_catalog() -> List[dict]:
    """Return the flat list of Scryfall set records, following pagination."""
    print(f"[sets] Fetching {SCRYFALL_SETS_URL} ...")
    sets: List[dict] = []
    url = SCRYFALL_SETS_URL
    while url:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        sets.extend(data.get("data", []))
        url = data.get("next_page")
        if url:
            time.sleep(SCRYFALL_RATE_LIMIT_S)
    print(f"[sets] Got {len(sets)} set records")
    return sets


def download_svg(set_code: str, svg_uri: str, force: bool = False) -> Optional[str]:
    """Save the raw SVG for a set code. Returns path or None on failure."""
    out_path = os.path.join(SVG_DIR, f"{set_code}.svg")
    if os.path.exists(out_path) and not force:
        return out_path
    try:
        resp = requests.get(svg_uri, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[svg] {set_code}: download failed: {e}")
        return None
    with open(out_path, "wb") as f:
        f.write(resp.content)
    return out_path


def rasterize_svg(svg_path: str, size: int) -> Optional[np.ndarray]:
    """Render SVG into an HxW RGB numpy array (white background) at the
    requested tall-size. Width scales proportionally. Returns None if
    svglib can't parse the file.

    reportlab's renderPM does not produce a transparent alpha channel on
    Windows, so we render against a pure-white background and let
    monochrome_normalize() recover the icon shape by inverting luminance.
    """
    try:
        drawing = svg2rlg(svg_path)
    except Exception as e:
        print(f"[raster] {os.path.basename(svg_path)}: svg2rlg failed: {e}")
        return None
    if drawing is None or drawing.height <= 0:
        return None

    scale = size / float(drawing.height)
    drawing.width *= scale
    drawing.height *= scale
    drawing.scale(scale, scale)

    try:
        pil = renderPM.drawToPIL(drawing, bg=0xFFFFFF)
    except Exception as e:
        print(f"[raster] {os.path.basename(svg_path)}: renderPM failed: {e}")
        return None
    if pil.mode != "RGB":
        pil = pil.convert("RGB")
    return np.array(pil)


def monochrome_normalize(rgb: np.ndarray) -> np.ndarray:
    """Convert RGB (white-bg, colored icon) -> single-channel mask where
    brighter pixels = icon, darker = background.

    Scryfall renders rarity-tinted icons for common/uncommon/rare. The
    *shape* is what we match against, not the fill color. We collapse to
    luminance, then invert so the icon ends up near 255 and the (pure-
    white) background ends up at 0 — the same convention as a clean alpha
    channel, which Canny and matchTemplate both prefer.
    """
    if rgb.ndim == 3 and rgb.shape[2] >= 3:
        gray = cv2.cvtColor(rgb[:, :, :3], cv2.COLOR_RGB2GRAY)
    else:
        gray = rgb
    return 255 - gray


def make_edge_template(mono: np.ndarray) -> np.ndarray:
    return cv2.Canny(mono, CANNY_LOW, CANNY_HIGH)


def render_one(
    set_code: str,
    svg_path: str,
    force: bool = False,
) -> bool:
    """Produce the png/edge rasters for every size. Returns True on success."""
    any_written = False
    for size in RASTER_SIZES:
        png_path = os.path.join(PNG_DIR, f"{set_code}_{size}.png")
        edge_path = os.path.join(EDGE_DIR, f"{set_code}_{size}.png")
        if os.path.exists(png_path) and os.path.exists(edge_path) and not force:
            continue
        rgba = rasterize_svg(svg_path, size=size)
        if rgba is None:
            return False
        mono = monochrome_normalize(rgba)
        cv2.imwrite(png_path, mono)
        edges = make_edge_template(mono)
        cv2.imwrite(edge_path, edges)
        any_written = True
    return True


def compute_confusable_pairs(
    png_dir: str = PNG_DIR,
    size: int = 48,
    threshold: int = CONFUSABLE_HASH_DIST,
) -> List[Tuple[str, str, int]]:
    """Scan rasters at `size` and report pairs within Hamming distance
    `threshold` (64-bit phash). Output is sorted (closest pairs first)."""
    hashes: dict[str, imagehash.ImageHash] = {}
    for fn in sorted(os.listdir(png_dir)):
        if not fn.endswith(f"_{size}.png"):
            continue
        set_code = fn[: -len(f"_{size}.png")]
        img = Image.open(os.path.join(png_dir, fn)).convert("L")
        hashes[set_code] = imagehash.phash(img)

    keys = sorted(hashes.keys())
    confusable: List[Tuple[str, str, int]] = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            d = hashes[a] - hashes[b]
            if d <= threshold:
                confusable.append((a, b, d))
    confusable.sort(key=lambda t: (t[2], t[0], t[1]))
    return confusable


def save_confusable_report(pairs: Iterable[Tuple[str, str, int]]) -> None:
    payload = [{"set_a": a, "set_b": b, "phash_distance": d} for a, b, d in pairs]
    with open(CONFUSABLE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"[confusable] Wrote {len(payload)} flagged pairs -> "
          f"{os.path.relpath(CONFUSABLE_PATH, SCRIPT_DIR)}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Re-download and re-render everything.")
    parser.add_argument("--rasterize-only", action="store_true",
                        help="Skip network; rasterize SVGs already on disk.")
    args = parser.parse_args(argv)

    ensure_dirs()

    if args.rasterize_only:
        sets_to_process: List[Tuple[str, str]] = []
        for fn in sorted(os.listdir(SVG_DIR)):
            if fn.endswith(".svg"):
                set_code = fn[:-4]
                sets_to_process.append((set_code, os.path.join(SVG_DIR, fn)))
        print(f"[local] {len(sets_to_process)} SVGs already on disk")
    else:
        catalog = fetch_sets_catalog()
        sets_to_process = []
        downloaded = 0
        skipped = 0
        for rec in catalog:
            set_code = rec.get("code", "").lower()
            svg_uri = rec.get("icon_svg_uri")
            if not set_code or not svg_uri:
                continue
            existed = os.path.exists(os.path.join(SVG_DIR, f"{set_code}.svg"))
            path = download_svg(set_code, svg_uri, force=args.force)
            if path is None:
                continue
            if not existed or args.force:
                downloaded += 1
                time.sleep(SCRYFALL_RATE_LIMIT_S)
            else:
                skipped += 1
            sets_to_process.append((set_code, path))
        print(f"[svg] Downloaded {downloaded}, already had {skipped}")

    ok = 0
    failed: List[str] = []
    for set_code, svg_path in sets_to_process:
        if render_one(set_code, svg_path, force=args.force):
            ok += 1
        else:
            failed.append(set_code)
    print(f"[raster] Rendered {ok} sets, {len(failed)} failed")
    if failed:
        print(f"[raster] Failed sets: {', '.join(failed[:20])}"
              f"{' ...' if len(failed) > 20 else ''}")

    pairs = compute_confusable_pairs()
    save_confusable_report(pairs)
    print(f"[done] set_symbols cache -> "
          f"{os.path.relpath(SET_SYMBOLS_DIR, SCRIPT_DIR)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
