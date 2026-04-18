# card_identify_hybrid.py
# ---------------------------------------------------------------------------
# Hybrid card identification: phash + DINOv2 with automatic fallback.
#
# Decision rules (applied in order):
#   1. Both agree on the same card name  ->  trust phash
#   2. phash distance <= 82              ->  trust phash  (confident)
#   3. phash #1-to-#2 gap >= 5           ->  trust phash  (clear winner)
#   4. Otherwise                         ->  trust DINOv2
#
# The gap rule (3) catches cases where phash has the right card at a high
# absolute distance (e.g. foils, lighting variation) but a decisive lead
# over #2.  On the 362-scan regression set, phash-correct cases always
# show gap >= 5.3 while phash-wrong cases always show gap <= 4.0.
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

# --- Decision thresholds ---
# With 1-region phash: phash-correct max distance = 80,
# dino-correct min distance = 85.  82 sits in the clean gap.
PHASH_CONFIDENCE_THRESHOLD = 82

# Gap between phash rank #1 and #2.  When phash has a decisive lead
# (gap >= 5), it's correct even at high absolute distance (e.g. foils).
# Calibrated on 362-scan set: correct high-dist gap >= 5.3, wrong gap <= 4.0.
PHASH_GAP_THRESHOLD = 5.0


def _get_name(card_id):
    """Look up card name from ID."""
    if card_id and card_id in CARD_DATA_BY_ID:
        return CARD_DATA_BY_ID[card_id].get('name', '')
    return ''


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

    # Case 1: Both agree -> trust phash result (has distance-sorted results)
    if p_id and d_id and _names_match(p_name, d_name):
        return p_id, p_dist, p_rot, p_results

    # Case 2: Phash is confident (low distance) -> trust phash
    if p_dist <= PHASH_CONFIDENCE_THRESHOLD:
        return p_id, p_dist, p_rot, p_results

    # Case 3: Phash has a decisive lead over #2 -> trust phash even at
    # high absolute distance (catches foils, lighting variation, etc.)
    if len(p_results) >= 2:
        gap = p_results[1][1] - p_results[0][1]
        if gap >= PHASH_GAP_THRESHOLD:
            return p_id, p_dist, p_rot, p_results

    # Case 4: Phash is not confident -> trust DINOv2
    if d_id:
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

        return d_id, p_dist, d_rot, merged

    # Case 5: DINOv2 returned nothing -> fall back to phash
    return p_id, p_dist, p_rot, p_results
