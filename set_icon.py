# set_icon.py
# ---------------------------------------------------------------------------
# Phase 6 of the printing-disambiguation cascade: set-icon detection.
#
# Given a rectified 745x1040 card scan and a list of candidate set codes
# that share the same art, crop the frame-era-specific symbol ROI, run
# Canny, and template-match each candidate's cached edge template from
# `card_data/set_symbols/edge/{set}_{size}.png`. The winning set code is
# the one whose template scores best, subject to a confidence threshold
# and a margin requirement over the runner-up.
#
# Fills in the Stage 3 stub in printing_disambiguation._run_cascade.
# ---------------------------------------------------------------------------

import os
from functools import lru_cache
from typing import Iterable, List, Optional, Tuple

import cv2
import numpy as np

from set_symbol_roi import get_symbol_roi

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_EDGE_DIR = os.path.join(_SCRIPT_DIR, "card_data", "set_symbols", "edge")

# Template size to load from the asset cache. 32 px is the smallest rung
# of the pyramid built by fetch_set_symbols.py; it fits comfortably
# inside every frame-era ROI in set_symbol_roi.FRAME_ROI (smallest ROI
# is 70x45), leaving room for matchTemplate to slide.
TEMPLATE_SIZE = 32

# Canny thresholds for the ROI crop. Match the values used in
# fetch_set_symbols.make_edge_template so the scan edges and the
# template edges come from the same filter response.
CANNY_LOW = 100
CANNY_HIGH = 200

# Confidence required to commit the stage. Measured as the peak
# TM_CCOEFF_NORMED score from matchTemplate, after scale/rotation
# search. Tune from Phase 7 labeled data.
SET_ICON_THRESHOLD = 0.55

# Minimum gap between the top scorer and runner-up. If below, the
# stage returns (None, best_score) and the cascade falls through.
SET_ICON_MARGIN = 0.10

# Scale/rotation search grid. Small — the rectified scan should be
# close to the template pose; these just absorb perspective residual,
# minor card tilt, and rasterization scale mismatch.
SET_ICON_SCALES: Tuple[float, ...] = (0.9, 1.0, 1.1)
SET_ICON_ROTATIONS: Tuple[int, ...] = (-5, 0, 5)


@lru_cache(maxsize=2048)
def _load_template(set_code: str, size: int = TEMPLATE_SIZE) -> Optional[np.ndarray]:
    """Load a Canny-edge template for a set code, or None if missing."""
    path = os.path.join(_EDGE_DIR, f"{set_code.lower()}_{size}.png")
    if not os.path.exists(path):
        return None
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    return img


def _preprocess_roi(card_img: np.ndarray, roi: Tuple[int, int, int, int]) -> np.ndarray:
    """Crop the ROI and return its Canny-edge representation."""
    x, y, w, h = roi
    crop = card_img[y:y + h, x:x + w]
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return cv2.Canny(crop, CANNY_LOW, CANNY_HIGH)


def _transformed_templates(template: np.ndarray) -> Iterable[np.ndarray]:
    """Yield every (scale, rotation) variant of a template."""
    for scale in SET_ICON_SCALES:
        h, w = template.shape[:2]
        new_h = max(4, int(round(h * scale)))
        new_w = max(4, int(round(w * scale)))
        resized = cv2.resize(template, (new_w, new_h),
                             interpolation=cv2.INTER_AREA)
        for angle in SET_ICON_ROTATIONS:
            if angle == 0:
                yield resized
                continue
            m = cv2.getRotationMatrix2D((new_w / 2, new_h / 2), angle, 1.0)
            rotated = cv2.warpAffine(resized, m, (new_w, new_h),
                                     flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_CONSTANT,
                                     borderValue=0)
            yield rotated


def _best_score(edge_roi: np.ndarray, template: np.ndarray) -> float:
    """Max TM_CCOEFF_NORMED score across the scale/rotation grid."""
    best = -1.0
    for variant in _transformed_templates(template):
        vh, vw = variant.shape[:2]
        if vh > edge_roi.shape[0] or vw > edge_roi.shape[1]:
            continue
        # matchTemplate can error on edge cases (empty template, etc.)
        # Surface as "no signal" rather than propagate.
        result = cv2.matchTemplate(edge_roi, variant, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        if max_val > best:
            best = float(max_val)
    return best


def identify_set_icon(
    card_img: np.ndarray,
    candidate_set_codes: List[str],
    frame: Optional[str],
    frame_effects: Optional[Iterable[str]] = None,
    debug: bool = False,
) -> Tuple[Optional[str], float]:
    """Score each candidate set code by matching its cached edge
    template against the frame-era symbol ROI on the scan.

    :param card_img: 745x1040 BGR (or grayscale) numpy array.
    :param candidate_set_codes: subset of Scryfall set codes to compare.
    :param frame: Scryfall `card["frame"]` of the scan's *most likely*
        printing — drives the ROI pick. Callers should pass the frame
        that survived Stage 1 of the cascade.
    :param frame_effects: `card["frame_effects"]` of the scan's most
        likely printing. Borderless / showcase short-circuit to (None, 0.0).
    :param debug: unused today; reserved for future debug-artifact
        writes (edge ROI + per-candidate score dumps).
    :return: `(best_set_code, confidence)` where confidence is the
        peak match score. `best_set_code` is None when:
          - ROI lookup returns None (borderless, 1993 frame, etc.)
          - Fewer than 1 candidate has a cached template
          - Top score is below SET_ICON_THRESHOLD
          - Top-to-second gap is below SET_ICON_MARGIN
        In all None cases the cascade should fall through to the next
        stage, not treat it as a negative signal.
    """
    roi = get_symbol_roi(frame, frame_effects)
    if roi is None:
        return None, 0.0
    if not candidate_set_codes:
        return None, 0.0

    edge_roi = _preprocess_roi(card_img, roi)
    if edge_roi.size == 0 or np.count_nonzero(edge_roi) == 0:
        return None, 0.0

    scores: List[Tuple[str, float]] = []
    for set_code in candidate_set_codes:
        template = _load_template(set_code)
        if template is None:
            continue
        score = _best_score(edge_roi, template)
        scores.append((set_code, score))

    if not scores:
        return None, 0.0

    scores.sort(key=lambda t: t[1], reverse=True)
    top_code, top_score = scores[0]

    if top_score < SET_ICON_THRESHOLD:
        return None, top_score

    if len(scores) >= 2:
        _, runner_up = scores[1]
        if top_score - runner_up < SET_ICON_MARGIN:
            return None, top_score

    return top_code, top_score
