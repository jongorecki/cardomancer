# set_icon.py
# ---------------------------------------------------------------------------
# Phase 6 of the printing-disambiguation cascade: set-icon detection.
#
# Given a rectified 745x1040 card scan and a list of candidate set codes
# that share the same art, crop the frame-era-specific symbol ROI and
# score each candidate's averaged-from-PNGs template pair (edge + gray)
# with an ensemble of TM_CCOEFF_NORMED scores. The winning set code is
# the one whose combined score is highest, subject to a confidence
# threshold and a margin requirement over the runner-up.
#
# Templates live at
#   card_data/set_symbols/roi_templates/{set}_{frame}_edge.png
#   card_data/set_symbols/roi_templates/{set}_{frame}_gray.png
# built by build_png_roi_templates.py by averaging clean Scryfall PNGs
# per (set, frame) group and applying Canny for the edge version.
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
_TEMPLATE_DIR = os.path.join(_SCRIPT_DIR, "card_data", "set_symbols", "roi_templates")

# Canny thresholds for the ROI crop. Must match the values used in
# build_png_roi_templates.py so scan edges and template edges come from
# the same filter response.
CANNY_LOW = 100
CANNY_HIGH = 200

# Pad the scan crop by this many pixels on each side so matchTemplate
# has room to slide — templates are exactly ROI-sized.
ROI_PAD = 15

# Weight applied to the grayscale score when combining with the edge
# score in the ensemble. Empirically best (0.35) on a 420-scan
# unambiguous-illustration validation: 73.3% vs 69.0% edge-only,
# 65.0% gray-only.
GRAY_WEIGHT = 0.35

# Confidence required to commit the stage. Ensemble scores range in
# [0, 1 + GRAY_WEIGHT] = [0, 1.35]; validation correct-match scores
# typically sit above ~0.45 with margins over 0.08 between top and
# runner-up. Tune from Phase 7 labeled data.
SET_ICON_THRESHOLD = 0.40
SET_ICON_MARGIN = 0.05

# Scale/rotation search grid. Small — the rectified scan should be
# close to the template pose; these just absorb perspective residual,
# minor card tilt, and rasterization scale mismatch.
SET_ICON_SCALES: Tuple[float, ...] = (0.95, 1.0, 1.05)
SET_ICON_ROTATIONS: Tuple[int, ...] = (-4, 0, 4)


@lru_cache(maxsize=2048)
def _load_template_pair(
    set_code: str, frame: str,
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """Load (edge_template, gray_template) for a (set, frame).

    Returns (None, None) when either file is missing — caller skips
    the candidate rather than treating it as a negative signal.
    """
    edge_path = os.path.join(
        _TEMPLATE_DIR, f"{set_code.lower()}_{frame}_edge.png"
    )
    gray_path = os.path.join(
        _TEMPLATE_DIR, f"{set_code.lower()}_{frame}_gray.png"
    )
    if not (os.path.exists(edge_path) and os.path.exists(gray_path)):
        return None, None
    edge = cv2.imread(edge_path, cv2.IMREAD_GRAYSCALE)
    gray = cv2.imread(gray_path, cv2.IMREAD_GRAYSCALE)
    return edge, gray


def _preprocess_roi(
    card_img: np.ndarray, roi: Tuple[int, int, int, int],
) -> Tuple[np.ndarray, np.ndarray]:
    """Crop the ROI with pad, return (edge_crop, gray_crop)."""
    x, y, w, h = roi
    x0 = max(0, x - ROI_PAD)
    y0 = max(0, y - ROI_PAD)
    x1 = min(card_img.shape[1], x + w + ROI_PAD)
    y1 = min(card_img.shape[0], y + h + ROI_PAD)
    crop = card_img[y0:y1, x0:x1]
    if crop.ndim == 3:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    else:
        gray = crop
    edge = cv2.Canny(gray, CANNY_LOW, CANNY_HIGH)
    return edge, gray


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


def _best_score(scan_crop: np.ndarray, template: np.ndarray) -> float:
    """Max TM_CCOEFF_NORMED score across the scale/rotation grid.
    Returns -1.0 when no variant fits inside the scan crop."""
    best = -1.0
    for variant in _transformed_templates(template):
        vh, vw = variant.shape[:2]
        if vh >= scan_crop.shape[0] or vw >= scan_crop.shape[1]:
            continue
        result = cv2.matchTemplate(scan_crop, variant, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        if max_val > best:
            best = float(max_val)
    return best


def _ensemble_score(
    edge_crop: np.ndarray, gray_crop: np.ndarray,
    edge_template: np.ndarray, gray_template: np.ndarray,
) -> float:
    """Combined edge + GRAY_WEIGHT * gray score. Negative contributions
    are clamped to 0 so a single bad-matching channel can't drag the
    ensemble negative when the other channel matches well."""
    se = _best_score(edge_crop, edge_template)
    sg = _best_score(gray_crop, gray_template)
    if se < 0 and sg < 0:
        return -1.0
    return max(0.0, se) + GRAY_WEIGHT * max(0.0, sg)


def identify_set_icon(
    card_img: np.ndarray,
    candidate_set_codes: List[str],
    frame: Optional[str],
    frame_effects: Optional[Iterable[str]] = None,
    debug: bool = False,
) -> Tuple[Optional[str], float]:
    """Score each candidate set code by matching its PNG-averaged
    template pair (edge + gray) against the frame-era symbol ROI.

    :param card_img: 745x1040 BGR (or grayscale) numpy array.
    :param candidate_set_codes: subset of Scryfall set codes to compare.
    :param frame: Scryfall `card["frame"]` of the scan's *most likely*
        printing — drives the ROI pick and selects which (set, frame)
        template pair to load. Callers should pass the frame that
        survived Stage 1 of the cascade.
    :param frame_effects: `card["frame_effects"]` of the scan's most
        likely printing. Borderless / showcase short-circuit to (None, 0.0).
    :param debug: unused today; reserved for future debug-artifact dumps.
    :return: `(best_set_code, confidence)` where confidence is the
        ensemble score of the winner. `best_set_code` is None when:
          - ROI lookup returns None (borderless, 1993 frame, etc.)
          - No candidate has a cached template pair for this frame
          - Top score is below SET_ICON_THRESHOLD
          - Top-to-second gap is below SET_ICON_MARGIN
        In all None cases the cascade should fall through.
    """
    roi = get_symbol_roi(frame, frame_effects)
    if roi is None:
        return None, 0.0
    if not candidate_set_codes or frame is None:
        return None, 0.0

    edge_crop, gray_crop = _preprocess_roi(card_img, roi)
    if edge_crop.size == 0 or np.count_nonzero(edge_crop) == 0:
        return None, 0.0

    scores: List[Tuple[str, float]] = []
    for set_code in candidate_set_codes:
        edge_tmpl, gray_tmpl = _load_template_pair(set_code, frame)
        if edge_tmpl is None or gray_tmpl is None:
            continue
        score = _ensemble_score(edge_crop, gray_crop, edge_tmpl, gray_tmpl)
        if score < 0:
            continue
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
