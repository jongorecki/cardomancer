# create_card_hashes_v2.py
# ---------------------------------------------------------------------------
# Build the v2 hash database: art-only crop + multi-hash (phash/dhash/whash).
#
# Improvements over v1:
#   1) Art-only crop — excludes card border, title, type line, text box.
#      Only the unique artwork is hashed, dramatically reducing false matches
#      between cards that share frame elements.
#   2) Multi-hash — phash (DCT), dhash (gradient), whash (wavelet) per channel.
#      Different hash algorithms capture different features; combining them
#      increases separation between different cards.
#   3) CLAHE normalization — same as v1, normalizes lighting differences.
#
# Usage:
#   python create_card_hashes_v2.py
#
# Takes ~15-30 minutes for ~49k cards depending on CPU cores.
# Output: card_hashes_v2.json
# ---------------------------------------------------------------------------

import os
import json
import time
import cv2
import numpy as np
from PIL import Image
import imagehash
from concurrent.futures import ProcessPoolExecutor

# Art regions (pixel coords on 745x1043 Scryfall images)
ART_REGION = (30, 105, 715, 520)        # Normal/adventure/planeswalker
ART_REGION_SAGA = (350, 60, 715, 600)   # Saga: art on right
ART_REGION_CLASS = (30, 60, 350, 600)   # Class/Case: art on left
# Battle: art occupies left ~2/3, defense strip + battle text on right 1/3.
# Must match ART_REGION_BATTLE in config.py.
ART_REGION_BATTLE = (30, 105, 500, 620)

# Layout types that need their own art region.
#
# NOTE: "battle" is NOT a real Scryfall layout — battles live under
# layout=transform (or occasionally layout=normal for some on-card battles).
# We key on the English type_line containing "Battle" at build time (see
# main()) to tag them, regardless of Scryfall layout.
LAYOUT_ART_REGIONS = {
    "saga": ART_REGION_SAGA,
    "class": ART_REGION_CLASS,
    "case": ART_REGION_CLASS,
    "battle": ART_REGION_BATTLE,
}

# CLAHE settings — must match hashing.py _apply_clahe_pil()
CLAHE_CLIP_LIMIT = 2.0
CLAHE_GRID_SIZE = 8

# Hash size (16 = 256 bits per hash)
HASH_SIZE = 16

# Back-face filename suffix — must match download_cards._BACK_FACE_SUFFIX.
# Files named `{card_id}__back.png` are the reverse sides of double-faced
# cards (transform/modal_dfc/reversible/meld). We hash them just like any
# other card but record a `canonical_id` so matches at runtime transparently
# resolve back to the real card_id.
BACK_FACE_SUFFIX = '__back'


def apply_clahe(pil_img):
    """Apply CLAHE to a PIL RGB image."""
    img_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)

    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT,
                             tileGridSize=(CLAHE_GRID_SIZE, CLAHE_GRID_SIZE))
    l_corrected = clahe.apply(l_ch)

    corrected_lab = cv2.merge([l_corrected, a_ch, b_ch])
    corrected_bgr = cv2.cvtColor(corrected_lab, cv2.COLOR_LAB2BGR)
    corrected_rgb = cv2.cvtColor(corrected_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(corrected_rgb)


def _hash_art(img_pil, art_region, hash_size):
    """Crop to art region, apply CLAHE, compute phash/dhash/whash per channel."""
    w, h = img_pil.size
    x1, y1, x2, y2 = art_region
    art_img = img_pil.crop((x1, y1, min(x2, w), min(y2, h)))
    art_img = apply_clahe(art_img)
    r, g, b = art_img.split()
    return {
        "p_r": str(imagehash.phash(r, hash_size=hash_size)),
        "p_g": str(imagehash.phash(g, hash_size=hash_size)),
        "p_b": str(imagehash.phash(b, hash_size=hash_size)),
        "d_r": str(imagehash.dhash(r, hash_size=hash_size)),
        "d_g": str(imagehash.dhash(g, hash_size=hash_size)),
        "d_b": str(imagehash.dhash(b, hash_size=hash_size)),
        "w_r": str(imagehash.whash(r, hash_size=hash_size)),
        "w_g": str(imagehash.whash(g, hash_size=hash_size)),
        "w_b": str(imagehash.whash(b, hash_size=hash_size)),
    }


def process_image(args):
    """Process a single card image: crop art, apply CLAHE, compute multi-hashes.
    Uses layout-specific art region for sagas, classes, etc.

    For back-face files (`{card_id}__back.png`), the returned key is the
    full stem including the suffix, but the entry also carries a
    `canonical_id` field pointing at the real card_id so that matches
    resolve back to the canonical card at runtime.
    """
    filename, images_dir, hash_size, layout = args
    file_stem = os.path.splitext(filename)[0]
    image_path = os.path.join(images_dir, filename)

    # Detect back-face file and compute canonical id.
    if file_stem.endswith(BACK_FACE_SUFFIX):
        canonical_id = file_stem[:-len(BACK_FACE_SUFFIX)]
    else:
        canonical_id = None  # normal file — no indirection needed

    try:
        with Image.open(image_path) as img:
            img = img.convert('RGB')

            # Use layout-specific art region if applicable
            art_region = LAYOUT_ART_REGIONS.get(layout, ART_REGION)
            hashes = _hash_art(img, art_region, hash_size)

            # Store the layout so matching knows which region was used
            if layout and layout != "normal":
                hashes["layout"] = layout

            if canonical_id is not None:
                hashes["canonical_id"] = canonical_id

            return file_stem, hashes, None

    except Exception as e:
        return None, None, f"Failed to process {filename}: {e}"


def _build_layout_map(cards_json):
    """Build {card_id: layout_string} from the Scryfall bulk data.

    Used to attach a per-card art-region hint to each process_image()
    call. Two passes:
      1. Scryfall-native layouts (saga / class / case) tagged by `layout`.
      2. Battles tagged by type_line containing "Battle" — battles
         ship under layout=transform on Scryfall, so the layout key
         alone misses them.
    """
    layout_map = {}
    if not os.path.exists(cards_json):
        return layout_map
    with open(cards_json, 'r', encoding='utf-8') as f:
        cards_data = json.load(f)
    for card in cards_data:
        cid = card.get('id', '')
        if not cid:
            continue
        layout = card.get('layout', 'normal')

        # Pass 1: native Scryfall layout tags (saga/class/case)
        if layout in LAYOUT_ART_REGIONS and layout != "battle":
            layout_map[cid] = layout
            continue

        # Pass 2: battle detection by type_line. Check top-level
        # type_line first; for multi-face cards (transform-layout
        # battles), Scryfall puts the type on card_faces[0].type_line.
        type_line = (card.get('type_line') or "").lower()
        if not type_line:
            faces = card.get('card_faces') or []
            if faces:
                type_line = (faces[0].get('type_line') or "").lower()
        if "battle" in type_line:
            layout_map[cid] = "battle"
    return layout_map


def create_v2_hash_database(images_dir, output_json, cards_json=None,
                             hash_size=None, max_workers=None,
                             progress_callback=None):
    """Generate the v2 hash DB at `output_json` from images in
    `images_dir`. Uses the layout-aware art regions when `cards_json`
    points at a Scryfall bulk file.

    Args:
        images_dir:        directory of .png card images
        output_json:       path to write the resulting hash DB JSON
        cards_json:        optional Scryfall bulk JSON path (gives
                           per-card layout hints for sagas / classes /
                           cases / battles). When None, every card
                           uses the normal art region.
        hash_size:         override HASH_SIZE (default: module constant)
        max_workers:       passed to ProcessPoolExecutor
        progress_callback: optional fn(idx, total, message) called
                           every 5000 cards. Used by the web_database
                           updater to emit SocketIO progress events.

    Returns the number of cards successfully hashed.
    """
    if hash_size is None:
        hash_size = HASH_SIZE
    if not os.path.isdir(images_dir):
        raise FileNotFoundError(f"images_dir not found: {images_dir}")

    layout_map = _build_layout_map(cards_json) if cards_json else {}
    if layout_map:
        counts = {k: sum(1 for v in layout_map.values() if v == k)
                  for k in ("saga", "class", "case", "battle")}
        print(f"[v2-hash] {len(layout_map)} cards with non-standard "
              f"layouts (saga: {counts['saga']}, class: {counts['class']}, "
              f"case: {counts['case']}, battle: {counts['battle']})")
    elif cards_json:
        print(f"[v2-hash] cards JSON {cards_json!r} missing — all "
              f"cards will use the normal art region")

    files = [f for f in os.listdir(images_dir) if f.lower().endswith('.png')]
    total = len(files)
    print(f"[v2-hash] {total} PNG files in {images_dir}")

    def _layout_for(filename):
        stem = os.path.splitext(filename)[0]
        if stem.endswith(BACK_FACE_SUFFIX):
            stem = stem[:-len(BACK_FACE_SUFFIX)]
        return layout_map.get(stem, "normal")

    args_list = [(f, images_dir, hash_size, _layout_for(f)) for f in files]
    hash_db = {}
    errors = 0
    start_time = time.time()

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        for idx, result in enumerate(
                executor.map(process_image, args_list), start=1):
            card_id, card_hashes, error_msg = result
            if error_msg:
                errors += 1
                if errors <= 20:
                    print(f"  ERROR: {error_msg}")
            else:
                hash_db[card_id] = card_hashes

            if idx % 5000 == 0 or idx == total:
                elapsed = time.time() - start_time
                rate = idx / elapsed
                eta = (total - idx) / rate if rate > 0 else 0
                msg = (f"[{idx}/{total}] {idx/total*100:.1f}% "
                       f"({rate:.0f} cards/sec, ETA {eta:.0f}s)")
                print(msg)
                if progress_callback is not None:
                    try:
                        progress_callback(idx, total, msg)
                    except Exception:
                        pass  # don't let a logging callback kill the build

    elapsed = time.time() - start_time
    print(f"[v2-hash] Done in {elapsed:.1f}s "
          f"({total/elapsed:.0f} cards/sec). "
          f"Processed: {len(hash_db)}, errors: {errors}")

    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(hash_db, f, ensure_ascii=False)
    size_mb = os.path.getsize(output_json) / (1024 * 1024)
    print(f"[v2-hash] Saved {output_json} ({size_mb:.1f} MB, "
          f"{len(hash_db)} entries)")
    return len(hash_db)


def main():
    """CLI entry point — keeps the hardcoded paths for standalone
    invocation. The web app calls create_v2_hash_database() directly
    with kiosk-specific paths."""
    create_v2_hash_database(
        images_dir="downloaded_cards",
        output_json="card_hashes_v2.json",
        cards_json="default-cards-20260315090814.json",
        hash_size=HASH_SIZE,
    )


if __name__ == "__main__":
    main()
