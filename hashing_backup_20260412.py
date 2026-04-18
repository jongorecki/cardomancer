# hashing.py
# ---------------------------------------------------------------------------
# Card hash matching with multi-hash fusion and two-stage re-ranking.
#
# V2 improvements:
#   1) Art-only crop — only the unique artwork is hashed
#   2) Multi-hash fusion — phash + dhash + whash per RGB channel
#   3) Two-stage re-ranking — fast hash for top candidates, then histogram
#      correlation on the art region to re-rank
#   4) CLAHE normalization — handles lighting variation
#
# Falls back to v1 hash DB if v2 is not available.
# ---------------------------------------------------------------------------

import os
import json
import cv2
import numpy as np
from PIL import Image
import imagehash
from config import (HASH_DB_PATH, ART_REGION, ART_REGION_SAGA, ART_REGION_CLASS,
                    ART_REGION_BATTLE,
                    CARD_BACK_REF_PATH, CARD_BACK_DISTANCE_THRESHOLD)

# Try to load v2 hash DB first, fall back to v1
_HASH_DB_V2_PATH = os.path.join(os.path.dirname(HASH_DB_PATH), "card_hashes_v2.json")
_using_v2 = False

if os.path.exists(_HASH_DB_V2_PATH):
    with open(_HASH_DB_V2_PATH, 'r', encoding='utf-8') as f:
        HASH_DB = json.load(f)
    _using_v2 = True
    print(f"[hashing] Loaded v2 hash DB: {len(HASH_DB)} entries (art-only, multi-hash)")
elif os.path.exists(HASH_DB_PATH):
    with open(HASH_DB_PATH, 'r', encoding='utf-8') as f:
        HASH_DB = json.load(f)
    print(f"[hashing] Loaded v1 hash DB: {len(HASH_DB)} entries (legacy)")
else:
    HASH_DB = {}
    print("[hashing] WARNING: No hash database found!")


# ---------------------------------------------------------------------------
# Precompute hash objects for faster lookup, grouped by layout
# ---------------------------------------------------------------------------

# All hashes in one flat list (for normal matching — most cards)
PRECOMPUTED_HASHES = []
# Layout-specific hashes — only cards with that layout. The v2 build script
# tags saga/class/case/battle via Scryfall layout + type_line; the `case`
# layout is folded into `class` because it uses the same art region.
PRECOMPUTED_HASHES_BY_LAYOUT = {
    "normal": [],
    "saga": [],
    "class": [],
    "battle": [],
}

_back_face_count = 0

if _using_v2:
    # V2 format: phash + dhash + whash per R/G/B channel.
    #
    # The dict key is the filename stem (unique on disk). For back faces of
    # double-sided cards, the stem is `{real_id}__back` and the entry
    # carries a `canonical_id` field pointing at the real card_id. We
    # store the canonical id in the precomputed tuple so that when a back
    # face scores best, downstream code (CARD_DATA_BY_ID, extract_card_info,
    # sort-mode bin assignment) transparently sees the real card.
    for file_key, h in HASH_DB.items():
        try:
            canonical = h.get("canonical_id") or file_key
            entry = (
                canonical,
                # phash
                imagehash.hex_to_hash(h['p_r']),
                imagehash.hex_to_hash(h['p_g']),
                imagehash.hex_to_hash(h['p_b']),
                # dhash
                imagehash.hex_to_hash(h['d_r']),
                imagehash.hex_to_hash(h['d_g']),
                imagehash.hex_to_hash(h['d_b']),
                # whash
                imagehash.hex_to_hash(h['w_r']),
                imagehash.hex_to_hash(h['w_g']),
                imagehash.hex_to_hash(h['w_b']),
            )
            if canonical != file_key:
                _back_face_count += 1
            layout = h.get("layout", "normal")
            # Fold `case` into `class` — same art region, same bucket at
            # runtime. The v2 build script tags both "class" and "case"
            # cards with their respective layout strings; we merge here so
            # detect_card_layout() only needs to return "class".
            if layout == "case":
                layout = "class"
            PRECOMPUTED_HASHES.append(entry)
            if layout in PRECOMPUTED_HASHES_BY_LAYOUT:
                PRECOMPUTED_HASHES_BY_LAYOUT[layout].append(entry)
            else:
                PRECOMPUTED_HASHES_BY_LAYOUT["normal"].append(entry)
        except (ValueError, KeyError):
            pass
else:
    # V1 format: phash only per R/G/B channel
    for card_id, h in HASH_DB.items():
        r_str = h.get('r_phash')
        g_str = h.get('g_phash')
        b_str = h.get('b_phash')
        if r_str and g_str and b_str:
            try:
                entry = (
                    card_id,
                    imagehash.hex_to_hash(r_str),
                    imagehash.hex_to_hash(g_str),
                    imagehash.hex_to_hash(b_str),
                )
                PRECOMPUTED_HASHES.append(entry)
                PRECOMPUTED_HASHES_BY_LAYOUT["normal"].append(entry)
            except ValueError:
                pass

_layout_counts = {k: len(v) for k, v in PRECOMPUTED_HASHES_BY_LAYOUT.items() if v}
print(f"[hashing] {len(PRECOMPUTED_HASHES)} cards ready for matching"
      f" (by layout: {_layout_counts})")
if _back_face_count:
    print(f"[hashing]   of which {_back_face_count} are back faces of "
          f"double-sided cards (resolve to canonical card_id)")


# ---------------------------------------------------------------------------
# Card back reference hash (precomputed at import time)
# ---------------------------------------------------------------------------

_CARD_BACK_HASHES = None

if os.path.exists(CARD_BACK_REF_PATH):
    _back_img = Image.open(CARD_BACK_REF_PATH).convert('RGB')
    # Use art region crop — resistant to border/corner variation from camera
    x1, y1, x2, y2 = ART_REGION
    _back_art = _back_img.crop((x1, y1, min(x2, _back_img.width), min(y2, _back_img.height)))
    _br, _bg, _bb = _back_art.split()
    _CARD_BACK_HASHES = (
        imagehash.phash(_br, hash_size=16),
        imagehash.phash(_bg, hash_size=16),
        imagehash.phash(_bb, hash_size=16),
    )
    del _back_img, _back_art, _br, _bg, _bb
    print("[hashing] Card back reference loaded (art-region hash)")
else:
    print(f"[hashing] WARNING: No card back reference at {CARD_BACK_REF_PATH}")
    print("[hashing]   To enable back detection, save a 745x1043 card back image there.")


def _hash_art_region(img_pil):
    """Compute per-channel phash of the art region."""
    x1, y1, x2, y2 = ART_REGION
    art = img_pil.crop((x1, y1, min(x2, img_pil.width), min(y2, img_pil.height)))
    r, g, b = art.split()
    return (
        imagehash.phash(r, hash_size=16),
        imagehash.phash(g, hash_size=16),
        imagehash.phash(b, hash_size=16),
    )


def _back_distance(hashes):
    """Average per-channel phash distance against card back reference."""
    ref_r, ref_g, ref_b = _CARD_BACK_HASHES
    return ((hashes[0] - ref_r) + (hashes[1] - ref_g) + (hashes[2] - ref_b)) / 3.0


def is_card_back(card_img):
    """
    Check if the given card image is the back of a card.
    Checks both upright and 180° rotated orientations.
    Returns (is_back, distance) using the better of the two.
    card_img: BGR numpy array (745x1043) or PIL Image.
    """
    if _CARD_BACK_HASHES is None:
        return False, 999

    if isinstance(card_img, np.ndarray):
        img_pil = Image.fromarray(cv2.cvtColor(card_img, cv2.COLOR_BGR2RGB))
    else:
        img_pil = card_img.convert('RGB')

    # Check upright
    dist_up = _back_distance(_hash_art_region(img_pil))

    # Check rotated 180°
    rotated = img_pil.rotate(180)
    dist_rot = _back_distance(_hash_art_region(rotated))

    dist = min(dist_up, dist_rot)
    return dist <= CARD_BACK_DISTANCE_THRESHOLD, dist


# ---------------------------------------------------------------------------
# CLAHE helper
# ---------------------------------------------------------------------------

def _apply_clahe_pil(img_pil, clip_limit=2.0, grid_size=8):
    """Apply CLAHE to a PIL RGB image, returning a new PIL image."""
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
# Art region cropping
# ---------------------------------------------------------------------------

_LAYOUT_ART_REGIONS = {
    "normal": ART_REGION,
    "saga": ART_REGION_SAGA,
    "class": ART_REGION_CLASS,
    "battle": ART_REGION_BATTLE,
}


def crop_art_region(card_img, layout="normal"):
    """
    Crop the art region from a card image (BGR numpy array or PIL Image).
    Uses layout-specific art regions for sagas, classes, etc.
    Returns a PIL RGB image of just the artwork.
    """
    x1, y1, x2, y2 = _LAYOUT_ART_REGIONS.get(layout, ART_REGION)

    if isinstance(card_img, np.ndarray):
        # BGR numpy array from OpenCV
        h, w = card_img.shape[:2]
        y2_clamped = min(y2, h)
        x2_clamped = min(x2, w)
        art_bgr = card_img[y1:y2_clamped, x1:x2_clamped]
        art_rgb = cv2.cvtColor(art_bgr, cv2.COLOR_BGR2RGB)
        return Image.fromarray(art_rgb)
    else:
        # PIL Image
        w, h = card_img.size
        return card_img.crop((x1, y1, min(x2, w), min(y2, h)))


# ---------------------------------------------------------------------------
# V2: Multi-hash matching (art-only, phash + dhash with CLAHE)
# ---------------------------------------------------------------------------

def _hash_art_for_search(art_pil, hash_size=16):
    """
    Apply CLAHE + compute phash + dhash per RGB channel for a pre-cropped
    art image. Returns a 6-tuple (r_ph, g_ph, b_ph, r_dh, g_dh, b_dh) that
    can be fed to `_distances_against_bucket()`.

    Separated so the multi-crop merge path can compute these hashes once
    per crop and then run the inner compare loop against a specific bucket
    without redoing the CLAHE+hash work.
    """
    art_pil = art_pil.convert('RGB')
    corrected = _apply_clahe_pil(art_pil)
    r, g, b = corrected.split()
    return (
        imagehash.phash(r, hash_size=hash_size),
        imagehash.phash(g, hash_size=hash_size),
        imagehash.phash(b, hash_size=hash_size),
        imagehash.dhash(r, hash_size=hash_size),
        imagehash.dhash(g, hash_size=hash_size),
        imagehash.dhash(b, hash_size=hash_size),
    )


def _distances_against_bucket(query_hashes, bucket):
    """
    Inner compare loop. `query_hashes` is the 6-tuple from
    `_hash_art_for_search`. `bucket` is a list of precomputed-hash entries
    like PRECOMPUTED_HASHES_BY_LAYOUT["class"].

    Returns an unsorted list of (card_id, combined_distance).
    """
    r_ph, g_ph, b_ph, r_dh, g_dh, b_dh = query_hashes
    results = []
    for entry in bucket:
        cid = entry[0]
        s_pr, s_pg, s_pb = entry[1], entry[2], entry[3]
        s_dr, s_dg, s_db = entry[4], entry[5], entry[6]
        d_phash = ((r_ph - s_pr) + (g_ph - s_pg) + (b_ph - s_pb)) / 3.0
        d_dhash = ((r_dh - s_dr) + (g_dh - s_dg) + (b_dh - s_db)) / 3.0
        results.append((cid, (d_phash + d_dhash) / 2.0))
    return results


def _compute_v2_distances(art_pil, hash_size=16, layout="normal"):
    """
    Compute phash + dhash distances against cards in the v2 database.
    Uses art-only crop with CLAHE, computes phash + dhash per RGB channel.

    Legacy entry point used by the single-crop paths. For non-normal
    layouts, searches only the matching layout bucket (no normal fallback
    — cross-layout recovery happens at the caller via multi-crop merge).

    Returns list of (card_id, combined_distance) sorted best-first.
    """
    query = _hash_art_for_search(art_pil, hash_size)

    if layout != "normal" and PRECOMPUTED_HASHES_BY_LAYOUT.get(layout):
        bucket = PRECOMPUTED_HASHES_BY_LAYOUT[layout]
    else:
        bucket = PRECOMPUTED_HASHES_BY_LAYOUT.get("normal", PRECOMPUTED_HASHES)

    results = _distances_against_bucket(query, bucket)
    results.sort(key=lambda x: x[1])
    return results


# ---------------------------------------------------------------------------
# V1: Legacy phash matching (full crop, phash only)
# ---------------------------------------------------------------------------

def _compute_v1_distances(img_pil, hash_size=16):
    """Legacy v1: phash-only with CLAHE, using the old 745x745 crop."""
    img_pil = img_pil.convert('RGB')

    # Hash original
    r1, g1, b1 = img_pil.split()
    r1_ph = imagehash.phash(r1, hash_size=hash_size)
    g1_ph = imagehash.phash(g1, hash_size=hash_size)
    b1_ph = imagehash.phash(b1, hash_size=hash_size)

    # Hash CLAHE-corrected
    corrected = _apply_clahe_pil(img_pil)
    r2, g2, b2 = corrected.split()
    r2_ph = imagehash.phash(r2, hash_size=hash_size)
    g2_ph = imagehash.phash(g2, hash_size=hash_size)
    b2_ph = imagehash.phash(b2, hash_size=hash_size)

    results = []
    for entry in PRECOMPUTED_HASHES:
        cid, s_r, s_g, s_b = entry[0], entry[1], entry[2], entry[3]
        d1 = ((r1_ph - s_r) + (g1_ph - s_g) + (b1_ph - s_b)) / 3.0
        d2 = ((r2_ph - s_r) + (g2_ph - s_g) + (b2_ph - s_b)) / 3.0
        combined = (d1 + d2) / 2.0
        results.append((cid, combined))

    results.sort(key=lambda x: x[1])
    return results


# ---------------------------------------------------------------------------
# Two-stage re-ranking using histogram correlation
# ---------------------------------------------------------------------------

def _compute_art_histogram(art_bgr):
    """
    Compute a color histogram for an art region image (BGR).
    Uses HSV color space for better perceptual matching.
    Returns a normalized histogram.
    """
    hsv = cv2.cvtColor(art_bgr, cv2.COLOR_BGR2HSV)
    # 32 bins for H, 32 for S, 16 for V — 16384 total bins
    hist = cv2.calcHist([hsv], [0, 1, 2], None,
                        [32, 32, 16],
                        [0, 180, 0, 256, 0, 256])
    cv2.normalize(hist, hist)
    return hist


def rerank_with_histograms(card_img, candidates, images_dir="downloaded_cards",
                           top_n=30):
    """
    Two-stage re-ranking: take the top-N hash candidates and re-rank them
    using histogram correlation on the art region.

    This is more expensive than hashing but much more accurate for
    discriminating between similar cards.

    :param card_img:    Full card image (BGR numpy array)
    :param candidates:  List of (card_id, hash_distance) from stage 1
    :param images_dir:  Directory containing reference card PNGs
    :param top_n:       How many candidates to re-rank
    :return:            Re-ranked list of (card_id, combined_score)
    """
    x1, y1, x2, y2 = ART_REGION
    h, w = card_img.shape[:2]

    # Crop art from the camera-captured card
    query_art = card_img[y1:min(y2, h), x1:min(x2, w)]
    query_hist = _compute_art_histogram(query_art)

    reranked = []
    for cid, hash_dist in candidates[:top_n]:
        # Load reference image
        ref_path = os.path.join(images_dir, f"{cid}.png")
        if not os.path.exists(ref_path):
            # Can't re-rank without reference image, keep hash distance
            reranked.append((cid, hash_dist))
            continue

        ref_img = cv2.imread(ref_path)
        if ref_img is None:
            reranked.append((cid, hash_dist))
            continue

        rh, rw = ref_img.shape[:2]
        ref_art = ref_img[y1:min(y2, rh), x1:min(x2, rw)]
        ref_hist = _compute_art_histogram(ref_art)

        # Histogram correlation: 1.0 = perfect match, -1.0 = inverse
        correlation = cv2.compareHist(query_hist, ref_hist, cv2.HISTCMP_CORREL)

        # Combined score: lower is better
        # Convert correlation to a distance (1 - corr), scale to be comparable
        # to hash distances, then average with the hash distance
        hist_dist = (1.0 - correlation) * 100.0  # scale to ~0-100 range
        combined = (hash_dist + hist_dist) / 2.0

        reranked.append((cid, combined))

    # Keep any remaining candidates beyond top_n unchanged
    reranked.extend(candidates[top_n:])
    reranked.sort(key=lambda x: x[1])
    return reranked


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_combined_distances(card_img, hash_size=16, layout="normal"):
    """
    Main entry point for card matching.

    If v2 hash DB is loaded:
      - Crops the card into EVERY art region (normal/saga/class/battle)
      - Computes phash + dhash + whash per RGB channel for each crop
      - Searches each crop against its matching layout bucket only
      - Merges: best distance per card across all crops wins
      - Returns sorted (card_id, distance) list

    If only v1 hash DB:
      - Uses legacy 745x745 crop with phash + CLAHE

    Why always multi-crop:
      The `layout` parameter is a *hint* from detect_card_layout(), which
      uses an HSV saturation profile that can misclassify in ~2-5% of
      cases — especially for tokens (no text box = saga-like profile)
      and battles with unusual art framing. If we trust the hint and
      search only that one bucket, a misclassification is unrecoverable.

      Running all four crops covers all four possibilities:
        1. class crop   vs class   bucket  (~45   entries)
        2. battle crop  vs battle  bucket  (~75   entries)
        3. saga crop    vs saga    bucket  (~200  entries)
        4. normal crop  vs normal  bucket  (~51k  entries)

      Total DB work ≈ sum of bucket sizes ≈ single normal-crop cost.
      The per-bucket search is meaningful because each runtime crop is
      compared against DB entries that were hashed with the SAME crop
      at build time. Cross-bucket comparisons (e.g., class crop vs
      normal-bucket hashes) produce noise and are avoided.

      Extra crops + CLAHE + hash work adds ~30-40 ms vs. single-crop.
      Well worth it for guaranteed recovery from layout misclassification.

    :param card_img: Either a full card image (BGR numpy array) or a
                     pre-cropped PIL image (for v1 backward compatibility)
    :param hash_size: Hash resolution (default 16)
    :param layout: Card layout hint from detect_card_layout(). Used for
                   logging/diagnostics only — the actual search always
                   covers all four layout buckets.
    :return: List of (card_id, combined_distance) sorted best-first
    """
    if _using_v2:
        # Multi-crop search across all four layouts. This is the only
        # path — we no longer trust the layout hint as a hard filter.
        class_art = crop_art_region(card_img, layout="class")
        battle_art = crop_art_region(card_img, layout="battle")
        normal_art = crop_art_region(card_img, layout="normal")
        saga_art = crop_art_region(card_img, layout="saga")

        class_q = _hash_art_for_search(class_art, hash_size)
        battle_q = _hash_art_for_search(battle_art, hash_size)
        normal_q = _hash_art_for_search(normal_art, hash_size)
        saga_q = _hash_art_for_search(saga_art, hash_size)

        all_results = [
            _distances_against_bucket(
                class_q, PRECOMPUTED_HASHES_BY_LAYOUT.get("class", [])),
            _distances_against_bucket(
                battle_q, PRECOMPUTED_HASHES_BY_LAYOUT.get("battle", [])),
            _distances_against_bucket(
                normal_q, PRECOMPUTED_HASHES_BY_LAYOUT.get("normal", [])),
            _distances_against_bucket(
                saga_q, PRECOMPUTED_HASHES_BY_LAYOUT.get("saga", [])),
        ]

        # Merge: best distance per card_id across all crops.
        best = {}
        for result_list in all_results:
            for cid, d in result_list:
                prev = best.get(cid)
                if prev is None or d < prev:
                    best[cid] = d
        merged = sorted(best.items(), key=lambda x: x[1])
        return merged
    else:
        # V1: legacy path — expects a PIL image
        if isinstance(card_img, np.ndarray):
            img_pil = Image.fromarray(cv2.cvtColor(card_img, cv2.COLOR_BGR2RGB))
        else:
            img_pil = card_img
        return _compute_v1_distances(img_pil, hash_size)


def compute_combined_distances_with_rerank(card_img, hash_size=16,
                                            rerank_top=30,
                                            images_dir="downloaded_cards",
                                            layout="normal"):
    """
    Card matching pipeline. Rerank was tested and found to hurt accuracy
    (50% vs 100%), so this now just calls compute_combined_distances directly.

    Kept for backward compatibility with callers that pass rerank params.
    """
    return compute_combined_distances(card_img, hash_size, layout=layout)


# ---------------------------------------------------------------------------
# Legacy API (backward compatibility)
# ---------------------------------------------------------------------------

def hash_image_color(img_pil, hash_size=16):
    """Legacy: compute best match using phash only."""
    results = compute_combined_distances(img_pil, hash_size)
    if results:
        return results[0]
    return None, float('inf')


def compute_distances_for_image(img_pil, hash_size=16):
    """Legacy: compute all distances using phash only."""
    return compute_combined_distances(img_pil, hash_size)
