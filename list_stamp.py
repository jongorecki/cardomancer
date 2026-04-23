# list_stamp.py
# Planeswalker-stamp detection for printing disambiguation.
#
# "The List" (set code `plst`) reprints cards with the same art as the
# original printing, but adds a planeswalker-symbol stamp just above
# the collector-info line in the bottom-left corner. Binary tiebreaker:
# stamp present -> a list-style reprint, stamp absent -> original
# printing.
#
# The same stamp is used by related list-style sets (notably `ulst`
# for Unfinity's list). This detector does not distinguish between
# those sets — it only answers "is a list-stamp visually present." The
# cascade's gating condition (Phase 3) scopes usage so that the
# stamp-answer only actually matters when phash candidates include a
# list-style set and a non-list set of the same art.
#
# Template lives at card_data/stamps/the_list.png (grayscale 28x28
# crop from a known-good plst source image). Matching is
# cv2.matchTemplate / TM_CCOEFF_NORMED on grayscale directly; Canny
# edges on the stamp are too sparse (~20 nonzero pixels) for reliable
# TM scoring, so we match the full grayscale instead.
#
# Not called by the primary detection / identification path.

import os
from typing import Iterable, Tuple

import cv2
import numpy as np

WIDTH = 745
HEIGHT = 1040

# Search ROI on the rectified 745x1040 card, tight around where the
# stamp appears. Widening this ROI substantially increases false
# positives because collector-line text shapes (digits, letters) can
# score highly against the stamp template.
#   (x, y, w, h)
THE_LIST_STAMP_ROI = (0, 968, 35, 30)

# Confidence threshold for committing to "has_stamp=True". Empirically
# tuned on 150 plst/ulst source images + 2000 non-list source images:
#   list-style  min score ~ 0.37 (DDR-layout outliers),  mean ~ 0.62
#   non-list    max score ~ 0.45 (rare outliers),        p95 < 0.01
# 0.35 gives high recall on list cards; the cascade gating condition
# (only runs when phash candidates include a list-style set) prevents
# the tail of non-list false positives from causing wrong answers.
LIST_STAMP_THRESHOLD = 0.35

# Scryfall set codes that print the planeswalker stamp and share art
# with non-stamped originals. Used by is_list_candidate_pair() to
# decide whether Stage 2 of the cascade should fire at all.
LIST_LIKE_SETS = frozenset({"plst", "ulst"})

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(
    _SCRIPT_DIR, "card_data", "stamps", "the_list.png"
)

_template_cache = None


def _load_template():
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    tpl = cv2.imread(TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        raise FileNotFoundError(
            f"List stamp template not found at {TEMPLATE_PATH}"
        )
    _template_cache = tpl
    return tpl


def detect_list_stamp(
    card_img: np.ndarray,
    debug: bool = False,
) -> Tuple[bool, float]:
    """
    Detect whether the scan has a list-style planeswalker stamp.

    :param card_img: BGR numpy array. Resized to 745x1040 if not
        already. Must be a rectified card (post-perspective-warp).
    :param debug: if True, writes the ROI crop to
        tmp_list_stamp_debug_roi.png for inspection (best-effort).
    :return: (has_stamp, confidence). has_stamp = confidence >=
        LIST_STAMP_THRESHOLD. Confidence is the max
        TM_CCOEFF_NORMED score in roughly [-1, 1]; clamp to that
        range if using as a probability.
    """
    if card_img.shape[:2] != (HEIGHT, WIDTH):
        card_img = cv2.resize(card_img, (WIDTH, HEIGHT))

    x, y, w, h = THE_LIST_STAMP_ROI
    roi = card_img[y:y + h, x:x + w]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

    template = _load_template()
    th, tw = template.shape[:2]
    if gray.shape[0] < th or gray.shape[1] < tw:
        return False, 0.0

    result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(result)
    confidence = float(max_val)

    if debug:
        try:
            cv2.imwrite("tmp_list_stamp_debug_roi.png", roi)
        except Exception:
            pass

    return confidence >= LIST_STAMP_THRESHOLD, confidence


def is_list_candidate_pair(candidate_cards: Iterable[dict]) -> bool:
    """
    Return True iff the candidate list contains both a list-style
    printing (plst / ulst / ...) and at least one non-list printing.

    Gating condition for Stage 2 of the Phase 3 cascade: if every
    candidate is list-style, or none is, there's nothing for this
    detector to disambiguate.

    :param candidate_cards: iterable of Scryfall card dicts (as stored
        in cards.CARD_DATA_BY_ID).
    """
    has_list = False
    has_other = False
    for c in candidate_cards:
        if c.get("set") in LIST_LIKE_SETS:
            has_list = True
        else:
            has_other = True
        if has_list and has_other:
            return True
    return False
