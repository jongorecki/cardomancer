# card_identify_hybrid.py
# ---------------------------------------------------------------------------
# Hybrid card identification: phash + DINOv2 with automatic fallback.
#
# Decision rules (applied in order):
#   1. Both agree on the same card name  ->  trust phash
#   2. phash distance <= 82              ->  trust phash  (confident)
#   3. phash dist <= 85 AND gap >= 5     ->  trust phash  (near-confident + clear winner)
#   4. Otherwise                         ->  trust DINOv2
#
# The gap rule (3) catches cases where phash has the right card at a
# slightly-elevated distance (e.g. foils, lighting variation) but a
# decisive lead over #2.  Capped at dist <= 85 because at higher
# distances (90+), even large gaps just mean "least bad noise match"
# (e.g. Common Curve Filler's all-black art matched dark cards at 90+
# with gap 7+ but was completely wrong).
#
# Drop-in replacement for card_identify.py — same API:
#   identify_card(card_img, threshold) -> (card_id, distance, rotated, results)
#   is_card_back(card_img)             -> (is_back, distance)
#
# Performance: ~320ms/card total (phash ~150ms + DINOv2 ~170ms)
# Accuracy:    100% on 362-scan regression set (97.2% overall, 0 wrong)
# ---------------------------------------------------------------------------

import os
import time

# Phash identifier (packed uint8, region-A only)
from card_identify import (
    identify_card as _phash_identify,
    is_card_back,                       # re-export as-is
    _load_db as _phash_load,
)

# DINOv2 identifier
from card_identify_v2 import (
    identify_card as _dino_identify,
)

from cards import CARD_DATA_BY_ID
import cards as _cards_mod  # for dynamic getattr of get_cheapest_printing_id

# --- Decision thresholds ---
# With 1-region phash: phash-correct max distance = 80,
# dino-correct min distance = 85.  82 sits in the clean gap.
PHASH_CONFIDENCE_THRESHOLD = 82

# Gap between phash rank #1 and #2.  When phash has a decisive lead
# (gap >= 5) at moderate distance (<=85), it's correct even above the
# main confidence threshold (e.g. foils).
# Calibrated on 362-scan set: correct high-dist gap >= 5.3, wrong gap <= 4.0.
# Capped at 85 to prevent false positives from blank-art trap cards (90+).
PHASH_GAP_THRESHOLD = 5.0
PHASH_GAP_MAX_DIST = 85


def _get_name(card_id):
    """Look up card name from ID."""
    if card_id and card_id in CARD_DATA_BY_ID:
        return CARD_DATA_BY_ID[card_id].get('name', '')
    return ''


def _remap_to_cheapest(card_id):
    """
    Remap an identified card_id to the cheapest nonfoil printing sharing the
    same art (illustration_id group).  Until we do set detection, this is how
    we avoid misclassifying a $285 serialized Fynn as high-value when the same
    art is also printed as a 50-cent regular.

    Uses getattr(cards_mod, ...) so we degrade gracefully if the running
    cards module predates this helper (function missing -> no remap).
    """
    if not card_id:
        return card_id
    fn = getattr(_cards_mod, 'get_cheapest_printing_id', None)
    if fn is None:
        return card_id
    try:
        return fn(card_id) or card_id
    except Exception:
        return card_id


def _names_match(name_a, name_b):
    """Fuzzy check if two card names refer to the same card."""
    if not name_a or not name_b:
        return False
    a = name_a.lower().strip()
    b = name_b.lower().strip()
    if a == b:
        return True
    # Handle partial matches (e.g. adventure cards truncated)
    if a.startswith(b[:15]) or b.startswith(a[:15]):
        return True
    if a in b or b in a:
        return True
    return False


def identify_card(card_img, threshold=None):
    """
    Hybrid phash + DINOv2 card identification.

    Runs both methods, picks the best answer using confidence-based
    decision logic.  Returns the same 4-tuple as card_identify.py:

    :param card_img: 745x1040 BGR numpy array or PIL Image
    :param threshold: Max phash distance for a match (default: 120)
    :return: (card_id, distance, was_rotated, all_results)
             card_id is None if no match.
             distance is phash distance (for backward compat / CSV logging).
             all_results is sorted [(card_id, distance)] — phash results
             when phash wins, or re-topped with DINOv2 pick when DINOv2 wins.
    """
    # --- Run phash ---
    p_id, p_dist, p_rot, p_results = _phash_identify(card_img, threshold)
    p_name = _get_name(p_id)

    # --- Run DINOv2 ---
    d_id, d_sim, d_rot, d_results = _dino_identify(card_img)
    d_name = _get_name(d_id)

    # --- Decision logic ---
    # Single-exit pattern so we can cheapest-printing-remap in one place.

    # Case 1: Both agree -> trust phash result (has distance-sorted results)
    if p_id and d_id and _names_match(p_name, d_name):
        final_id, final_dist, final_rot, final_results = p_id, p_dist, p_rot, p_results

    # Case 2: Phash is confident (low distance) -> trust phash
    elif p_dist <= PHASH_CONFIDENCE_THRESHOLD:
        final_id, final_dist, final_rot, final_results = p_id, p_dist, p_rot, p_results

    # Case 3: Phash has a decisive lead over #2 at moderate distance ->
    # trust phash (catches foils, lighting variation).
    # Only applies at dist <= 85; at 90+ a big gap just means noise.
    elif (p_dist <= PHASH_GAP_MAX_DIST and len(p_results) >= 2
          and (p_results[1][1] - p_results[0][1]) >= PHASH_GAP_THRESHOLD):
        final_id, final_dist, final_rot, final_results = p_id, p_dist, p_rot, p_results

    # Case 4: Phash is not confident -> trust DINOv2
    elif d_id:
        # Build all_results with DINOv2's pick at position 0.
        # Use a synthetic low distance so it passes the threshold check
        # in web_worker's filtering code.
        dino_dist = max(0.0, (1.0 - d_sim) * 200.0)  # sim 0.8 -> 40, sim 0.6 -> 80

        # Start with the DINOv2 pick, then append phash results
        # (excluding the DINOv2 pick if it appears in phash results)
        merged = [(d_id, dino_dist)]
        for cid, d in p_results:
            if cid != d_id:
                merged.append((cid, d))

        final_id, final_dist, final_rot, final_results = d_id, p_dist, d_rot, merged

    # Case 5: DINOv2 returned nothing -> fall back to phash
    else:
        final_id, final_dist, final_rot, final_results = p_id, p_dist, p_rot, p_results

    # --- Cheapest-printing remap ---
    # Remap the winning card_id to the cheapest sibling with the same art,
    # so a serialized / promo / expensive-variant printing doesn't push the
    # card into the wrong value bin.  Also rewrite the top entry of
    # final_results so downstream consumers (CSV logging, UI) see the
    # remapped card everywhere.
    remapped_id = _remap_to_cheapest(final_id)
    if remapped_id != final_id and final_results:
        final_results = [(remapped_id, final_results[0][1])] + final_results[1:]
    final_id = remapped_id

    return final_id, final_dist, final_rot, final_results
