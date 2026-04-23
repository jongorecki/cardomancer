# printing_disambiguation.py
# ---------------------------------------------------------------------------
# Cascade tiebreaker for same-art printings.
#
# Called AFTER identify_card() (phash + DINOv2) has produced a card_id.
# If the winning card shares its illustration_id with other paper/English
# printings, the cascade runs the following stages to narrow to a single
# printing:
#
#   Stage 1: Frame / frame_effects (frame_detect.py)
#   Stage 2: List-style stamp      (list_stamp.py)
#   Stage 3: Set icon              (TODO Phase 6 — stubbed here)
#
# Each stage runs only when the surviving candidate list still disagrees
# on that stage's axis. When the cascade cannot narrow to a single
# survivor, we fall back to the existing cheapest-printing remap
# (cards.get_cheapest_printing_id), scoped to the surviving set.
#
# Design goals:
#   - Orthogonal to identify_card() — callers invoke
#     disambiguate_printing(card_img, card_id) after identification.
#   - No modifications to identify_card()'s behavior or return shape.
#   - Core cascade logic (_run_cascade) is a pure function taking a
#     candidate list, so tests can exercise it without loading the full
#     Scryfall bulk data.
# ---------------------------------------------------------------------------

from typing import List, Optional

import numpy as np

from cards import (
    CARD_DATA_BY_ID,
    get_illustration_id,
    get_same_illustration_english_candidates,
)
from frame_detect import detect_frame, frame_combo_from_card
from list_stamp import (
    LIST_LIKE_SETS,
    LIST_STAMP_THRESHOLD,
    detect_list_stamp,
    is_list_candidate_pair,
)
from set_icon import identify_set_icon

# Confidence required to commit Stage 1's frame pick. Frame detection
# returns confidence = margin/scale, so ~0.6 means a 18-hash-distance
# gap between best and second-best combo.
FRAME_THRESHOLD = 0.60

# Source labels (value of result["source"]) are ordered from most-
# informative to fallback. Tests and UI use these verbatim.
SOURCE_SINGLE_MATCH = "single_match"
SOURCE_NO_ART_GROUP = "no_art_group"
SOURCE_NO_CANDIDATES = "no_candidates"
SOURCE_FRAME_DISAMBIGUATED = "frame_disambiguated"
SOURCE_STAMP_DISAMBIGUATED = "stamp_disambiguated"
SOURCE_ICON_DISAMBIGUATED = "icon_disambiguated"
SOURCE_FULLY_DISAMBIGUATED = "fully_disambiguated"
SOURCE_CHEAPEST_FALLBACK = "cheapest_fallback"


def disambiguate_printing(
    card_img: np.ndarray,
    card_id: str,
    debug: bool = False,
) -> dict:
    """
    Resolve which printing a scan represents, given phash/DINOv2's
    card_id and the rectified card image.

    :param card_img: 745x1040 BGR numpy array. The rectified card scan.
    :param card_id: the card_id returned by identify_card().
    :param debug: if True, detectors write debug artifacts to disk.
    :return: dict with:
        final_card_id           str | None   — disambiguated printing
        source                  str          — one of the SOURCE_* labels
        frame_confidence        float | None — Stage 1 confidence (if ran)
        frame_pick              tuple | None — (frame, frame_effects_tuple)
        stamp_confidence        float | None — Stage 2 confidence (if ran)
        stamp_has_stamp         bool | None  — Stage 2 decision
        icon_confidence         float | None — Stage 3 confidence (if ran)
        icon_set_pick           str | None   — Stage 3 winning set_code
        candidates_considered   list[str]    — same-art candidate ids at entry
        candidates_surviving    list[str]    — ids after cascade filtering
    """
    if not card_id:
        return _empty_result(card_id=None, source=SOURCE_NO_CANDIDATES)

    illus = get_illustration_id(card_id)
    if not illus:
        return _empty_result(card_id=card_id, source=SOURCE_NO_ART_GROUP)

    candidate_ids = get_same_illustration_english_candidates(illus)
    seen = set()
    candidate_ids = [c for c in candidate_ids if not (c in seen or seen.add(c))]

    candidate_cards = [CARD_DATA_BY_ID.get(cid) for cid in candidate_ids]
    candidate_cards = [c for c in candidate_cards if c]

    return _run_cascade(card_img, card_id, candidate_cards, debug=debug)


def _empty_result(card_id: Optional[str], source: str) -> dict:
    return {
        "final_card_id": card_id,
        "source": source,
        "frame_confidence": None,
        "frame_pick": None,
        "stamp_confidence": None,
        "stamp_has_stamp": None,
        "icon_confidence": None,
        "icon_set_pick": None,
        "candidates_considered": [],
        "candidates_surviving": [],
    }


def _run_cascade(
    card_img: np.ndarray,
    card_id: str,
    candidate_cards: List[dict],
    debug: bool = False,
) -> dict:
    """
    Core cascade logic. Pure-ish (no Scryfall lookups by id), so tests
    can pass any candidate list.

    :param card_img: 745x1040 BGR numpy array (or anything frame_detect
        / list_stamp will accept).
    :param card_id: the id identify_card() returned. Used as fallback
        result when cascade yields nothing.
    :param candidate_cards: same-art candidate dicts. If 0 or 1 entry,
        the cascade is a no-op.
    :param debug: passed through to detectors.
    """
    candidate_ids = [c.get("id") for c in candidate_cards]

    if len(candidate_cards) <= 1:
        return {
            "final_card_id": card_id,
            "source": SOURCE_SINGLE_MATCH,
            "frame_confidence": None,
            "frame_pick": None,
            "stamp_confidence": None,
            "stamp_has_stamp": None,
            "icon_confidence": None,
            "icon_set_pick": None,
            "candidates_considered": candidate_ids,
            "candidates_surviving": candidate_ids,
        }

    surviving: List[dict] = list(candidate_cards)
    frame_confidence: Optional[float] = None
    frame_pick = None
    stamp_confidence: Optional[float] = None
    stamp_has_stamp: Optional[bool] = None
    icon_confidence: Optional[float] = None
    icon_set_pick: Optional[str] = None
    frame_committed = False
    stamp_committed = False
    icon_committed = False

    # --- Stage 1: frame / frame_effects ---
    frames = {frame_combo_from_card(c) for c in surviving}
    if len(frames) >= 2:
        combos = list(frames)
        fr = detect_frame(card_img, candidate_combos=combos, debug=debug)
        frame_confidence = fr["confidence"]
        if fr["best_frame"] is not None:
            frame_pick = (
                fr["best_frame"],
                tuple(fr["best_frame_effects"] or ()),
            )
            if frame_confidence >= FRAME_THRESHOLD:
                surviving = [c for c in surviving
                             if frame_combo_from_card(c) == frame_pick]
                frame_committed = True

    # --- Stage 2: list-style stamp ---
    if is_list_candidate_pair(surviving):
        has_stamp, stamp_confidence = detect_list_stamp(
            card_img, debug=debug
        )
        stamp_has_stamp = has_stamp
        # Commit only on a confident yes (>= threshold). Commit on a
        # confident no when the detector score is below half of
        # threshold — gives a symmetric "clearly negative" signal.
        if stamp_confidence >= LIST_STAMP_THRESHOLD:
            surviving = [c for c in surviving
                         if c.get("set") in LIST_LIKE_SETS]
            stamp_committed = True
        elif stamp_confidence < (LIST_STAMP_THRESHOLD / 2.0):
            surviving = [c for c in surviving
                         if c.get("set") not in LIST_LIKE_SETS]
            stamp_committed = True

    # --- Stage 3: set icon ---
    # Runs only if more than one surviving candidate disagrees on
    # `set`. We pass the most likely frame + frame_effects (either
    # what Stage 1 committed, or the first surviving candidate's
    # combo) so the ROI lookup is consistent.
    surviving_sets = {c.get("set") for c in surviving}
    if len(surviving_sets) >= 2:
        if frame_pick is not None:
            icon_frame = frame_pick[0]
            icon_frame_effects = frame_pick[1]
        else:
            icon_frame = surviving[0].get("frame")
            icon_frame_effects = surviving[0].get("frame_effects") or ()
        candidate_set_codes = sorted(
            {c.get("set") for c in surviving if c.get("set")}
        )
        icon_set_pick, icon_confidence = identify_set_icon(
            card_img,
            candidate_set_codes,
            icon_frame,
            icon_frame_effects,
            debug=debug,
        )
        if icon_set_pick is not None:
            surviving = [c for c in surviving
                         if c.get("set") == icon_set_pick]
            icon_committed = True

    surviving_ids = [c.get("id") for c in surviving]

    # --- Decide source label ---
    committed_count = sum(
        1 for x in (frame_committed, stamp_committed, icon_committed) if x
    )
    if len(surviving) == 1:
        final_id = surviving[0].get("id")
        if committed_count >= 2:
            source = SOURCE_FULLY_DISAMBIGUATED
        elif icon_committed:
            source = SOURCE_ICON_DISAMBIGUATED
        elif stamp_committed:
            source = SOURCE_STAMP_DISAMBIGUATED
        elif frame_committed:
            source = SOURCE_FRAME_DISAMBIGUATED
        else:
            # Only one candidate survived, but we didn't actually
            # commit any detector — the candidate list was already
            # narrow or detectors were low-confidence but a single
            # candidate remained after filtering.
            source = SOURCE_SINGLE_MATCH
    elif len(surviving) == 0:
        # Detectors filtered everything out. Unlikely if thresholds
        # are right; fall back to cheapest across the original art
        # group (NOT the surviving set, since it's empty).
        final_id = _cheapest_among(candidate_cards) or card_id
        source = SOURCE_CHEAPEST_FALLBACK
    else:
        # Multiple survivors — pick the cheapest among them.
        final_id = _cheapest_among(surviving) or card_id
        source = SOURCE_CHEAPEST_FALLBACK

    return {
        "final_card_id": final_id,
        "source": source,
        "frame_confidence": frame_confidence,
        "frame_pick": frame_pick,
        "stamp_confidence": stamp_confidence,
        "stamp_has_stamp": stamp_has_stamp,
        "icon_confidence": icon_confidence,
        "icon_set_pick": icon_set_pick,
        "candidates_considered": candidate_ids,
        "candidates_surviving": surviving_ids,
    }


def _cheapest_among(cards_: List[dict]) -> Optional[str]:
    """Pick the cheapest-nonfoil id from a card-dict list, or None."""
    best_id = None
    best_price = float("inf")
    for c in cards_:
        raw = (c.get("prices") or {}).get("usd")
        if not raw:
            continue
        try:
            p = float(raw)
        except (ValueError, TypeError):
            continue
        if p > 0 and p < best_price:
            best_price = p
            best_id = c.get("id")
    return best_id
