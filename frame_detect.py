# frame_detect.py
# Frame / frame_effects detection for printing disambiguation.
#
# Used as a tiebreaker AFTER phash has identified the card's art but
# returned multiple candidate printings that differ on frame era or
# frame_effects (retro, borderless, showcase, extendedart, etc.).
#
# Loads precomputed border-region phash signatures from
# frame_signatures.json (built by find_frame_signatures.py) and scores
# a scan against the candidate (frame, frame_effects) combos that phash
# surfaced.
#
# Not called by the primary detection / identification path.

import json
import os
from typing import Iterable, Optional

import cv2
import imagehash
import numpy as np
from PIL import Image

WIDTH = 745
HEIGHT = 1040

STABLE_REGIONS = {
    "top_border":    (0, 50, 0, WIDTH),
    "left_border":   (0, HEIGHT, 0, 50),
    "bottom_border": (HEIGHT - 50, HEIGHT, 0, WIDTH),
}

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FRAME_SIGNATURES_PATH = os.path.join(_SCRIPT_DIR, "frame_signatures.json")

# Hamming distance scale for confidence normalization. The summed
# distance across 3 regions has max theoretical value 3 * 64 = 192.
# Empirically, matching combos tend to sum under ~30 and mismatched
# combos over ~60, so a margin of ~30 between best and second-best is
# "very confident." Tune with diag data.
_CONFIDENCE_MARGIN_SCALE = 30.0

_signatures_cache = None


def _load_signatures():
    """Lazy-load frame_signatures.json and parse phash hex strings."""
    global _signatures_cache
    if _signatures_cache is not None:
        return _signatures_cache

    with open(FRAME_SIGNATURES_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    parsed = {}
    for frame, effects_map in raw.items():
        parsed[frame] = {}
        for fe_str, regions in effects_map.items():
            parsed[frame][fe_str] = {
                region: [imagehash.hex_to_hash(h) for h in hashes]
                for region, hashes in regions.items()
            }
    _signatures_cache = parsed
    return parsed


def _frame_effects_key(frame_effects):
    """Convert a frame_effects list/tuple/None to the 'fe_str' JSON key."""
    if not frame_effects:
        return ""
    return ",".join(sorted(frame_effects))


def _compute_scan_region_phashes(card_img):
    """
    Compute phash for each stable border region of a rectified card.

    :param card_img: BGR numpy array. Resized to 745x1040 if not already.
    :return: dict[region_name, imagehash.ImageHash]
    """
    if card_img.shape[:2] != (HEIGHT, WIDTH):
        card_img = cv2.resize(card_img, (WIDTH, HEIGHT))
    rgb = cv2.cvtColor(card_img, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)

    out = {}
    for name, (y0, y1, x0, x1) in STABLE_REGIONS.items():
        crop = pil.crop((x0, y0, x1, y1))
        out[name] = imagehash.phash(crop)
    return out


def _min_distance(query_hash, reference_hashes):
    """Minimum Hamming distance between query and any reference. None if empty."""
    if not reference_hashes:
        return None
    return min(query_hash - r for r in reference_hashes)


def detect_frame(
    card_img: np.ndarray,
    candidate_combos: Optional[Iterable] = None,
    debug: bool = False,
) -> dict:
    """
    Score a rectified card image against frame / frame_effects combos.

    :param card_img: 745x1040 BGR numpy array (post-rectification).
        If not exactly that size, it will be resized. Must be in BGR
        order (same as cv2.imread default).
    :param candidate_combos: optional iterable of (frame_str, frame_effects)
        pairs, where frame_effects is a list/tuple/None. If provided,
        scoring is restricted to those combos — the normal use case when
        phash has returned a candidate printing list. If None, scores
        against every combo present in frame_signatures.json.
    :param debug: if True, include per-combo scores in the result.
    :return: dict with keys:
        best_frame          str or None           ("2015", "1997", ...)
        best_frame_effects  tuple[str, ...] or None
        distance            int or None           summed Hamming across 3 regions
        margin              float                 gap to second-best combo
        confidence          float in [0, 1]       margin / scale
        scores              dict[(frame, fe_tuple), int] | None   debug only
    """
    signatures = _load_signatures()
    scan_phashes = _compute_scan_region_phashes(card_img)

    # Build the list of combos to score against.
    combos_to_score = []
    if candidate_combos is not None:
        seen = set()
        for frame, frame_effects in candidate_combos:
            fe_tuple = tuple(sorted(frame_effects)) if frame_effects else ()
            key = (frame, fe_tuple)
            if key in seen:
                continue
            seen.add(key)
            fe_key = _frame_effects_key(fe_tuple)
            if frame in signatures and fe_key in signatures[frame]:
                combos_to_score.append((frame, fe_tuple, fe_key))
    else:
        for frame, effects_map in signatures.items():
            for fe_str in effects_map.keys():
                fe_tuple = tuple(fe_str.split(",")) if fe_str else ()
                combos_to_score.append((frame, fe_tuple, fe_str))

    scores = {}
    for frame, fe_tuple, fe_key in combos_to_score:
        region_refs = signatures[frame][fe_key]
        total = 0
        ok = True
        for region_name, scan_hash in scan_phashes.items():
            d = _min_distance(scan_hash, region_refs.get(region_name, []))
            if d is None:
                ok = False
                break
            total += d
        if ok:
            scores[(frame, fe_tuple)] = total

    if not scores:
        return {
            "best_frame": None,
            "best_frame_effects": None,
            "distance": None,
            "margin": 0.0,
            "confidence": 0.0,
            "scores": scores if debug else None,
        }

    sorted_scores = sorted(scores.items(), key=lambda kv: kv[1])
    best_combo, best_dist = sorted_scores[0]

    if len(sorted_scores) > 1:
        second_dist = sorted_scores[1][1]
        margin = float(second_dist - best_dist)
    else:
        # Only one candidate — no discriminative signal. Report the pick
        # but give it neutral confidence (caller can still use it; the
        # candidate list came from phash and is authoritative).
        margin = 0.0

    confidence = max(0.0, min(1.0, margin / _CONFIDENCE_MARGIN_SCALE))

    return {
        "best_frame": best_combo[0],
        "best_frame_effects": best_combo[1],
        "distance": int(best_dist),
        "margin": margin,
        "confidence": confidence,
        "scores": scores if debug else None,
    }


def frame_combo_from_card(card: dict) -> tuple:
    """
    Helper: extract the (frame, frame_effects_tuple) pair from a
    Scryfall card dict (as stored in cards.CARD_DATA_BY_ID).
    """
    frame = card.get("frame")
    fe = card.get("frame_effects") or []
    return (frame, tuple(sorted(fe)))
