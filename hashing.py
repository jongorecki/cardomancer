# hashing.py
# ---------------------------------------------------------------------------
# DEPRECATED — This entire module is NO LONGER USED for card identification.
# The new system lives in card_identify.py (with card_hashes_v3.json).
# This file is kept only because some diagnostic endpoints in web_server.py
# still reference it. A backup copy is at backup/hashing_DEPRECATED.py.
# ---------------------------------------------------------------------------
# Card identification via per-channel perceptual hashing.
#
# This module provides the complete card identification pipeline:
#   1. Card-back detection (phash against reference image)
#   2. Orientation detection (0° vs 180° — pick the better match)
#   3. Card identification (per-channel phash on top 745x745 crop)
#
# The approach is deliberately simple and proven reliable:
#   - Crop the top 745x745 pixels from a 745x1040 canonical card image.
#     This region contains the title bar, artwork, and type line — enough
#     visual uniqueness to identify any card regardless of layout (normal,
#     saga, class, battle). It works across all card border styles (old
#     border, modern border, borderless, extended art).
#   - Apply CLAHE normalization to handle lighting differences between
#     the webcam capture and the Scryfall reference images.
#   - Compute phash on each color channel (R, G, B) independently.
#     Per-channel hashing preserves color information that grayscale
#     hashing would destroy (e.g., red vs blue cards with similar art).
#   - Average the three channel distances for the final score.
#
# This matches how the v1 hash database (card_hashes.json) was built.
# ---------------------------------------------------------------------------

import os
import json
import cv2
import numpy as np
from PIL import Image
import imagehash
from config import (HASH_DB_PATH, CARD_BACK_REF_PATH,
                    CARD_BACK_DISTANCE_THRESHOLD,
                    ART_REGION, ART_REGION_SAGA, ART_REGION_CLASS,
                    ART_REGION_BATTLE, PHASH_DISTANCE_THRESHOLD)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CROP_SIZE = 745       # Top crop height (= card width, making a square)
HASH_SIZE = 16        # imagehash resolution (16x16 = 256-bit hashes)
CLAHE_CLIP = 2.0      # CLAHE clip limit
CLAHE_GRID = 8        # CLAHE grid size


# ---------------------------------------------------------------------------
# Hash database loading
# ---------------------------------------------------------------------------

# Load the v1 hash database (card_hashes.json).
# Format: { card_id: { r_phash: hex, g_phash: hex, b_phash: hex } }
HASH_DB = {}
if os.path.exists(HASH_DB_PATH):
    with open(HASH_DB_PATH, 'r', encoding='utf-8') as _f:
        HASH_DB = json.load(_f)
    print(f"[hashing] Loaded hash DB: {len(HASH_DB)} entries")
else:
    print(f"[hashing] WARNING: No hash database found at {HASH_DB_PATH}")


# Precompute hash objects for fast matching at runtime.
# Each entry: (card_id, r_phash, g_phash, b_phash)
PRECOMPUTED_HASHES = []
for _cid, _h in HASH_DB.items():
    _r_str = _h.get('r_phash')
    _g_str = _h.get('g_phash')
    _b_str = _h.get('b_phash')
    if _r_str and _g_str and _b_str:
        try:
            PRECOMPUTED_HASHES.append((
                _cid,
                imagehash.hex_to_hash(_r_str),
                imagehash.hex_to_hash(_g_str),
                imagehash.hex_to_hash(_b_str),
            ))
        except ValueError:
            pass

# Legacy compatibility — other modules reference this dict
PRECOMPUTED_HASHES_BY_LAYOUT = {
    "normal": PRECOMPUTED_HASHES,
    "saga": [],
    "class": [],
    "battle": [],
}

print(f"[hashing] {len(PRECOMPUTED_HASHES)} cards ready for matching")


# ---------------------------------------------------------------------------
# Card back reference (precomputed at import time)
# ---------------------------------------------------------------------------

_CARD_BACK_HASHES = None

if os.path.exists(CARD_BACK_REF_PATH):
    _back_img = Image.open(CARD_BACK_REF_PATH).convert('RGB')
    # Hash the top crop — same method as card matching
    _back_crop = _back_img.crop((0, 0, CROP_SIZE,
                                  min(CROP_SIZE, _back_img.height)))
    _br, _bg, _bb = _back_crop.split()
    _CARD_BACK_HASHES = (
        imagehash.phash(_br, hash_size=HASH_SIZE),
        imagehash.phash(_bg, hash_size=HASH_SIZE),
        imagehash.phash(_bb, hash_size=HASH_SIZE),
    )
    del _back_img, _back_crop, _br, _bg, _bb
    print("[hashing] Card back reference loaded")
else:
    print(f"[hashing] WARNING: No card back reference at {CARD_BACK_REF_PATH}")


# ---------------------------------------------------------------------------
# CLAHE normalization
# ---------------------------------------------------------------------------

def _apply_clahe_pil(img_pil, clip_limit=CLAHE_CLIP, grid_size=CLAHE_GRID):
    """
    Apply CLAHE (Contrast Limited Adaptive Histogram Equalization) to a
    PIL RGB image. This normalizes lighting so that webcam captures and
    Scryfall reference images produce similar hash values despite
    different lighting conditions.

    Operates on the L channel in LAB color space only — preserves
    chrominance (color) while normalizing luminance (brightness).
    """
    img_bgr = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)

    clahe = cv2.createCLAHE(clipLimit=clip_limit,
                             tileGridSize=(grid_size, grid_size))
    l_corrected = clahe.apply(l_ch)

    corrected_lab = cv2.merge([l_corrected, a_ch, b_ch])
    corrected_bgr = cv2.cvtColor(corrected_lab, cv2.COLOR_LAB2BGR)
    corrected_rgb = cv2.cvtColor(corrected_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(corrected_rgb)


# ---------------------------------------------------------------------------
# Core hashing functions
# ---------------------------------------------------------------------------

def _to_pil(card_img):
    """Convert a BGR numpy array to a PIL RGB image (pass-through if already PIL)."""
    if isinstance(card_img, np.ndarray):
        return Image.fromarray(cv2.cvtColor(card_img, cv2.COLOR_BGR2RGB))
    return card_img.convert('RGB')


def _hash_top_crop(card_pil):
    """
    Hash the top 745x745 crop of a card image.

    Steps:
      1. Crop top 745x745 pixels (title + art + type line)
      2. Apply CLAHE normalization (matching how the DB was built)
      3. Compute phash per R, G, B channel

    Returns: (r_phash, g_phash, b_phash) tuple of imagehash objects
    """
    crop = card_pil.crop((0, 0, CROP_SIZE,
                           min(CROP_SIZE, card_pil.height)))
    crop = _apply_clahe_pil(crop)
    r, g, b = crop.split()
    return (
        imagehash.phash(r, hash_size=HASH_SIZE),
        imagehash.phash(g, hash_size=HASH_SIZE),
        imagehash.phash(b, hash_size=HASH_SIZE),
    )


def _channel_distance(query_hashes, ref_hashes):
    """Average per-channel Hamming distance between two hash tuples."""
    return sum(query_hashes[i] - ref_hashes[i] for i in range(3)) / 3.0


def _match_all(hashes):
    """
    Compare a hash tuple against ALL entries in the precomputed DB.
    Returns sorted list of (card_id, distance), best first.
    """
    rh, gh, bh = hashes
    results = []
    for entry in PRECOMPUTED_HASHES:
        cid, sr, sg, sb = entry[0], entry[1], entry[2], entry[3]
        d = ((rh - sr) + (gh - sg) + (bh - sb)) / 3.0
        results.append((cid, d))
    results.sort(key=lambda x: x[1])
    return results


# ---------------------------------------------------------------------------
# Card back detection
# ---------------------------------------------------------------------------

def is_card_back(card_img):
    """
    Check if the given card image is a card back.

    Computes per-channel phash on the top crop and compares against the
    card back reference image. Checks both upright and 180° rotated
    orientations (card backs are nearly symmetric but not perfectly so).

    :param card_img: 745x1040 BGR numpy array or PIL Image
    :return: (is_back: bool, distance: float)
    """
    if _CARD_BACK_HASHES is None:
        return False, 999.0

    pil = _to_pil(card_img)

    # Check upright
    h_up = _hash_top_crop(pil)
    d_up = _channel_distance(h_up, _CARD_BACK_HASHES)

    # Check 180°
    rotated = pil.rotate(180)
    h_rot = _hash_top_crop(rotated)
    d_rot = _channel_distance(h_rot, _CARD_BACK_HASHES)

    dist = min(d_up, d_rot)
    return dist <= CARD_BACK_DISTANCE_THRESHOLD, dist


# ---------------------------------------------------------------------------
# Card identification (main entry point)
# ---------------------------------------------------------------------------

def identify_card(card_img, threshold=None):
    """
    Identify a card image by finding the best match in the hash database.

    Handles orientation automatically: tries both 0° and 180° rotations,
    picks whichever orientation produces a lower distance to its best
    match. This is the same approach used by the original reliable
    detection system.

    Pipeline:
      1. Convert to PIL
      2. Hash top 745x745 crop (upright)
      3. Hash top 745x745 crop (rotated 180°)
      4. Find best DB match for each orientation
      5. Pick the orientation with lower distance
      6. Return match if under threshold

    :param card_img: 745x1040 BGR numpy array or PIL Image
    :param threshold: Max distance to consider a match (default: PHASH_DISTANCE_THRESHOLD)
    :return: (card_id, distance, was_rotated) if matched,
             (None, best_distance, was_rotated) if no match
    """
    if threshold is None:
        threshold = PHASH_DISTANCE_THRESHOLD

    pil = _to_pil(card_img)

    # 0° orientation
    h_up = _hash_top_crop(pil)
    results_up = _match_all(h_up)
    best_up_id, best_up_dist = results_up[0] if results_up else (None, 999.0)

    # 180° orientation
    rotated = pil.rotate(180)
    h_rot = _hash_top_crop(rotated)
    results_rot = _match_all(h_rot)
    best_rot_id, best_rot_dist = results_rot[0] if results_rot else (None, 999.0)

    # Pick better orientation
    if best_up_dist <= best_rot_dist:
        best_id, best_dist, was_rotated = best_up_id, best_up_dist, False
        all_results = results_up
    else:
        best_id, best_dist, was_rotated = best_rot_id, best_rot_dist, True
        all_results = results_rot

    if best_dist <= threshold:
        return best_id, best_dist, was_rotated, all_results
    else:
        return None, best_dist, was_rotated, all_results


# ---------------------------------------------------------------------------
# Art region cropping (used by other modules)
# ---------------------------------------------------------------------------

_LAYOUT_ART_REGIONS = {
    "normal": ART_REGION,
    "saga": ART_REGION_SAGA,
    "class": ART_REGION_CLASS,
    "battle": ART_REGION_BATTLE,
}


def crop_art_region(card_img, layout="normal"):
    """
    Crop the art region from a card image.
    Uses layout-specific coordinates.

    :param card_img: BGR numpy array or PIL Image
    :param layout: "normal", "saga", "class", "battle"
    :return: PIL RGB image of the art region
    """
    x1, y1, x2, y2 = _LAYOUT_ART_REGIONS.get(layout, ART_REGION)

    if isinstance(card_img, np.ndarray):
        h, w = card_img.shape[:2]
        y2c = min(y2, h)
        x2c = min(x2, w)
        art_bgr = card_img[y1:y2c, x1:x2c]
        art_rgb = cv2.cvtColor(art_bgr, cv2.COLOR_BGR2RGB)
        return Image.fromarray(art_rgb)
    else:
        w, h = card_img.size
        return card_img.crop((x1, y1, min(x2, w), min(y2, h)))


# ---------------------------------------------------------------------------
# Legacy / backward-compatible API
# ---------------------------------------------------------------------------
# These functions are imported by other modules (main.py, test scripts,
# web_server.py). They wrap the new simple pipeline to maintain compatibility.

def compute_combined_distances(card_img, hash_size=16, layout="normal"):
    """
    Compute distances against all cards in the hash database.

    Legacy API — wraps the new simple matching. The layout parameter is
    accepted but ignored (the 745x745 top crop works for all layouts).

    :param card_img: BGR numpy array or PIL Image
    :param hash_size: Ignored (always uses HASH_SIZE=16)
    :param layout: Ignored (745x745 top crop covers all layouts)
    :return: Sorted list of (card_id, distance), best first
    """
    pil = _to_pil(card_img)
    hashes = _hash_top_crop(pil)
    return _match_all(hashes)


def compute_combined_distances_with_rerank(card_img, hash_size=16,
                                            rerank_top=30,
                                            images_dir="downloaded_cards",
                                            layout="normal"):
    """Legacy API — reranking removed, delegates to compute_combined_distances."""
    return compute_combined_distances(card_img, hash_size, layout=layout)


def hash_image_color(img_pil, hash_size=16):
    """Legacy: compute best match. Returns (card_id, distance)."""
    results = compute_combined_distances(img_pil, hash_size)
    if results:
        return results[0]
    return None, float('inf')


def compute_distances_for_image(img_pil, hash_size=16):
    """Legacy: compute all distances."""
    return compute_combined_distances(img_pil, hash_size)


def _hash_art_for_search(art_pil, hash_size=16):
    """
    Legacy API — compute per-channel phash for an art crop.
    Returns a 6-tuple for backward compatibility (phash + dhash per channel),
    but dhash values are set to the same as phash (dhash removed).
    """
    art_pil = art_pil.convert('RGB')
    corrected = _apply_clahe_pil(art_pil)
    r, g, b = corrected.split()
    r_ph = imagehash.phash(r, hash_size=hash_size)
    g_ph = imagehash.phash(g, hash_size=hash_size)
    b_ph = imagehash.phash(b, hash_size=hash_size)
    # Return 6-tuple for backward compat (no separate dhash)
    return (r_ph, g_ph, b_ph, r_ph, g_ph, b_ph)
