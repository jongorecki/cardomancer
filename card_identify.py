# card_identify.py
# ---------------------------------------------------------------------------
# Card identification via per-channel perceptual hashing (art region only).
#
# Uses Region A (30, 105, 715, 520) -- the standard art area -- in both
# upright and 180-degree rotated orientations (2 comparisons total).
# B/C regions dropped: DINOv2 hybrid covers the few cards they helped.
#
# Per-channel (R, G, B) phash with hash_size=16 (256-bit per channel).
# Hashes stored as packed uint8 arrays for fast XOR + popcount matching.
#
# DB loading: tries packed .npz cache first (~1s), falls back to JSON
# (~75s) and auto-creates the cache for next time.
# ---------------------------------------------------------------------------

import logging
import os
import json
import time
import cv2
import numpy as np
from PIL import Image
import imagehash

logger = logging.getLogger(__name__)

# --- Region definition (art crop, must match build_hash_db_v3.py Region A) ---
REGION_A = (30, 105, 715, 520)

# --- Back-face suffix (matches download_cards.py + build_hash_db_v3.py) ---
# The hash DB stores back faces of transform / MDFC / meld / reversible cards
# under keys like "{real_uuid}__back".  We strip the suffix at return time so
# downstream code sees the canonical front-face ID (which is what
# CARD_DATA_BY_ID uses) regardless of which side of the card was scanned.
_BACK_FACE_SUFFIX = '__back'


def _canonicalize(card_id):
    """Strip __back suffix -> canonical front-face ID (or pass through)."""
    if card_id and card_id.endswith(_BACK_FACE_SUFFIX):
        return card_id[:-len(_BACK_FACE_SUFFIX)]
    return card_id

# --- Hash size (must match build_hash_db_v3.py) ---
HASH_SIZE = 16  # 16x16 = 256-bit hashes

# --- Thresholds (scaled for 256-bit hashes) ---
MATCH_THRESHOLD = 120
CARD_BACK_THRESHOLD = 100

# --- DB paths ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HASH_DB_JSON = os.path.join(SCRIPT_DIR, 'card_hashes_v3.json')
HASH_DB_NPZ = os.path.join(SCRIPT_DIR, 'card_hashes_packed.npz')

# --- Popcount lookup table: byte value -> number of set bits ---
POPCOUNT_LUT = np.array([bin(i).count('1') for i in range(256)], dtype=np.uint32)

# ---------------------------------------------------------------------------
# DB loading -- packed uint8 storage
# ---------------------------------------------------------------------------

_card_ids = []              # list of card IDs, index-aligned with numpy arrays
_db_a_packed = None         # numpy uint8 shape (N, 3, 32) -- Region A packed
_card_back_packed = None    # numpy uint8 shape (3, 32) -- card back packed
_db_loaded = False


def _hash_to_bits(hex_str):
    """Convert a hex hash string to a flat boolean numpy array."""
    h = imagehash.hex_to_hash(hex_str)
    return h.hash.flatten()


def _pack_bits(bool_arr):
    """Pack bool array along last axis into uint8.  (..., 256) -> (..., 32)."""
    return np.packbits(bool_arr.astype(np.uint8), axis=-1)


def _load_db():
    """Load hash DB.  Uses packed npz cache when available, else JSON."""
    global _card_ids, _db_a_packed, _card_back_packed, _db_loaded

    if _db_loaded:
        return

    # ------------------------------------------------------------------
    # Fast path: load from packed npz cache
    # ------------------------------------------------------------------
    if os.path.exists(HASH_DB_NPZ):
        npz_fresh = True
        if os.path.exists(HASH_DB_JSON):
            npz_fresh = (os.path.getmtime(HASH_DB_NPZ)
                         >= os.path.getmtime(HASH_DB_JSON))

        if npz_fresh:
            t0 = time.time()
            data = np.load(HASH_DB_NPZ, allow_pickle=True)
            _card_ids = list(data['ids'])
            _db_a_packed = data['a_packed']        # (N, 3, 32) uint8
            if 'card_back_packed' in data:
                _card_back_packed = data['card_back_packed']  # (3, 32)
                logger.info("Card back reference loaded")
            elapsed = time.time() - t0
            logger.info(f"Loaded {len(_card_ids)} cards from "
                        f"packed cache ({elapsed:.1f}s)")
            _db_loaded = True
            return

    # ------------------------------------------------------------------
    # Slow path: load from JSON, extract Region A, pack, cache as npz
    # ------------------------------------------------------------------
    if not os.path.exists(HASH_DB_JSON):
        logger.warning(f"No hash DB at {HASH_DB_JSON}")
        _db_loaded = True
        return

    t0 = time.time()
    with open(HASH_DB_JSON, 'r', encoding='utf-8') as f:
        raw_db = json.load(f)

    # Extract card back
    card_back_entry = raw_db.pop('_card_back', None)
    if card_back_entry:
        cb_bits = np.stack([
            _hash_to_bits(card_back_entry['a_pr']),
            _hash_to_bits(card_back_entry['a_pg']),
            _hash_to_bits(card_back_entry['a_pb']),
        ])  # (3, 256) bool
        _card_back_packed = _pack_bits(cb_bits)  # (3, 32) uint8
        logger.info("Card back reference loaded")

    # Build packed numpy arrays -- Region A only
    n = len(raw_db)
    if n == 0:
        _db_loaded = True
        return

    bits_len = len(_hash_to_bits(next(iter(raw_db.values()))['a_pr']))

    ap = np.empty((n, 3, bits_len), dtype=bool)
    for i, (cid, entry) in enumerate(raw_db.items()):
        _card_ids.append(cid)
        ap[i, 0] = _hash_to_bits(entry['a_pr'])
        ap[i, 1] = _hash_to_bits(entry['a_pg'])
        ap[i, 2] = _hash_to_bits(entry['a_pb'])

    _db_a_packed = _pack_bits(ap)  # (N, 3, 32) uint8

    elapsed = time.time() - t0
    logger.info(f"Loaded {n} cards from JSON "
                f"({elapsed:.1f}s, {bits_len}-bit -> packed uint8)")

    # Cache as npz for fast loading next time
    try:
        save_kw = {
            'ids': np.array(_card_ids, dtype=object),
            'a_packed': _db_a_packed,
        }
        if _card_back_packed is not None:
            save_kw['card_back_packed'] = _card_back_packed
        np.savez(HASH_DB_NPZ, **save_kw)
        size_mb = os.path.getsize(HASH_DB_NPZ) / (1024 * 1024)
        logger.info(f"Cached packed DB -> "
                    f"{os.path.basename(HASH_DB_NPZ)} ({size_mb:.1f} MB)")
    except Exception as e:
        logger.warning(f"Could not cache npz: {e}")

    _db_loaded = True


# Load on import
_load_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_pil(card_img):
    """Convert BGR numpy array to PIL RGB.  Pass through if already PIL."""
    if isinstance(card_img, np.ndarray):
        return Image.fromarray(cv2.cvtColor(card_img, cv2.COLOR_BGR2RGB))
    return card_img.convert('RGB')


def _auto_levels(img_pil, black_pct=0.5):
    """
    Per-channel black-point correction using LUT remap.

    Finds the darkest `black_pct`% of pixels per channel and maps that
    value to 0, stretching the rest of the range to 0-255.  Uses a
    256-entry lookup table per channel for fast remapping.
    """
    arr = np.asarray(img_pil)          # uint8, no copy
    result = arr.copy()
    for ch in range(3):
        lo = int(np.percentile(arr[:, :, ch], black_pct))
        if lo < 1:
            continue
        lut = np.arange(256, dtype=np.float32)
        lut[:lo] = 0
        lut[lo:] = ((lut[lo:] - lo) * (255.0 / (255 - lo))).clip(0, 255)
        result[:, :, ch] = lut.astype(np.uint8)[arr[:, :, ch]]
    return Image.fromarray(result)


def _hash_region(img_pil, region):
    """Crop region, return per-channel phash as packed uint8 (3, 32)."""
    x1, y1, x2, y2 = region
    w, h = img_pil.size
    crop = img_pil.crop((x1, y1, min(x2, w), min(y2, h)))
    r, g, b = crop.split()
    bits = np.stack([
        imagehash.phash(r, hash_size=HASH_SIZE).hash.flatten(),
        imagehash.phash(g, hash_size=HASH_SIZE).hash.flatten(),
        imagehash.phash(b, hash_size=HASH_SIZE).hash.flatten(),
    ])  # (3, 256) bool
    return _pack_bits(bits)  # (3, 32) uint8


def _match_vectorized(query_packed, db_packed):
    """
    Compare query phash against all DB entries (packed XOR + popcount).

    :param query_packed: shape (3, 32) uint8
    :param db_packed:    shape (N, 3, 32) uint8
    :return: (best_index, best_distance, all_distances)
    """
    xor = np.bitwise_xor(db_packed, query_packed[np.newaxis, :, :])
    channel_dists = POPCOUNT_LUT[xor].sum(axis=2)          # (N, 3)
    avg_dists = channel_dists.mean(axis=1).astype(np.float32)  # (N,)

    best_idx = int(np.argmin(avg_dists))
    best_dist = float(avg_dists[best_idx])
    return best_idx, best_dist, avg_dists


# ---------------------------------------------------------------------------
# Card back detection
# ---------------------------------------------------------------------------

def is_card_back(card_img):
    """
    Check if the card image is a card back.

    :param card_img: 745x1040 BGR numpy array or PIL Image
    :return: (is_back: bool, distance: float)
    """
    if _card_back_packed is None:
        return False, 999.0

    pil = _auto_levels(_to_pil(card_img))

    def _back_dist(img_pil):
        query = _hash_region(img_pil, REGION_A)
        xor = np.bitwise_xor(query, _card_back_packed)
        return float(POPCOUNT_LUT[xor].sum(axis=1).mean())

    d_up = _back_dist(pil)
    d_rot = _back_dist(pil.rotate(180))
    dist = min(d_up, d_rot)
    return dist <= CARD_BACK_THRESHOLD, dist


# ---------------------------------------------------------------------------
# Card identification
# ---------------------------------------------------------------------------

def identify_card(card_img, threshold=None):
    """
    Identify a card by comparing Region A in 2 orientations against the DB.

    :param card_img: 745x1040 BGR numpy array or PIL Image
    :param threshold: Max distance for a match (default: MATCH_THRESHOLD)
    :return: (card_id, distance, was_rotated, all_results)
             card_id is None if no match under threshold.
             all_results is sorted [(card_id, distance)] from the winning
             orientation comparison.
    """
    if threshold is None:
        threshold = MATCH_THRESHOLD

    if _db_a_packed is None or len(_card_ids) == 0:
        return None, 999.0, False, []

    pil = _auto_levels(_to_pil(card_img))
    rotated = pil.rotate(180)

    # Hash Region A in both orientations
    h_up = _hash_region(pil, REGION_A)
    h_rot = _hash_region(rotated, REGION_A)

    # Match both against packed DB
    idx_up, dist_up, dists_up = _match_vectorized(h_up, _db_a_packed)
    idx_rot, dist_rot, dists_rot = _match_vectorized(h_rot, _db_a_packed)

    # Pick better orientation
    if dist_up <= dist_rot:
        best_idx, best_dist = idx_up, dist_up
        best_dists = dists_up
        was_rotated = False
    else:
        best_idx, best_dist = idx_rot, dist_rot
        best_dists = dists_rot
        was_rotated = True

    # Build sorted results from the winning comparison.
    # Canonicalize IDs: DFC back-face entries (keyed as "{id}__back" in the DB)
    # are remapped to their front-face canonical ID so every consumer downstream
    # (hybrid name-match, CARD_DATA_BY_ID lookup, cheapest-printing remap,
    # scan CSV logging) gets an ID that resolves in the Scryfall data.
    all_results = sorted(
        ((_canonicalize(cid), dist) for cid, dist in zip(_card_ids, best_dists.tolist())),
        key=lambda x: x[1]
    )

    best_id = _canonicalize(_card_ids[best_idx])

    if best_dist <= threshold:
        return best_id, best_dist, was_rotated, all_results
    else:
        return None, best_dist, was_rotated, all_results
