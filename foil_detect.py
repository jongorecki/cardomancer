# foil_detect.py
# ---------------------------------------------------------------------------
# Post-identification foil detection for scanned cards.
#
# Runs AFTER card_identify has matched a scan to a card ID. Uses the matched
# card's reference PNG (downloaded_cards/<card_id>.png) to distinguish "this
# specific scan is a foil printing" from "this card has bright saturated art".
#
# The approach: foils look NORMAL under diffuse reference-photo light, but
# under the LED scanner they develop specular rainbow hotspots that
# (a) brighten the image, (b) desaturate in the hotspot center, and
# (c) scatter hue across the spectrum in the bright regions.
#
# No single signal alone works — many non-foil cards (basic lands, full-art
# promos) have naturally bright saturated art and would trip a single-
# threshold detector. The reference-relative deltas correct for that.
#
# Three signals combine into a single confidence score:
#
#   S1: delta_bright_frac = bright_frac(scan) - bright_frac(ref)
#       Foils have MORE bright pixels than their reference due to specular
#       reflection. Typical foil: +0.02 to +0.15. Non-foil: ±0.005.
#
#   S2: delta_mean_s = mean_s_bright(scan) - mean_s_bright(ref)
#       Foils DESATURATE in specular hotspots (rainbow shimmer reads close
#       to white). Typical foil: -15 to -40. Non-foil: ±5.
#
#   S3: hue_range_bright = spread of hue values in bright pixels
#       Foils scatter hue across the rainbow (high spread). Non-foils
#       cluster around the art's dominant hue.
#
# Public API:
#   detect_foil(card_img, card_id=None, reference_img=None) -> dict
#
# Tuning note: default thresholds err on the side of under-detection.
# It's better to flag a foil as nonfoil than to flag every basic land as
# a foil. Use prototype_foil_multisignal.py to re-tune on your labeled set.
# ---------------------------------------------------------------------------

import os
from typing import Optional

import cv2
import numpy as np

from config import SCRIPT_DIR

# --- Paths ---
REFERENCE_DIR = os.path.join(SCRIPT_DIR, "downloaded_cards")

# --- Bright-pixel gate ---
BRIGHT_V_THRESH = 220          # HSV V above this = "bright pixel"
MIN_BRIGHT_PIXELS = 500        # need this many to trust the signal

# --- Score weights (tuned against session_20260415_130451 top-30) ---
# Normalized per-signal contributions; final score in roughly [-1, +1] range.
W_DELTA_BRIGHT_FRAC = 3.0      # multiplier on bright_frac delta
W_DELTA_MEAN_S      = -0.025   # negative: desaturation in bright pixels
                                # contributes POSITIVE foil score
W_HUE_RANGE_BRIGHT  = 0.005    # multiplier on hue spread in bright pixels

# --- Classification threshold ---
# confidence >= this -> is_foil = True
#
# Calibration against session_20260415_130451 (358 reference-comparable scans,
# 1 known foil at scan 205 = Zombie Infestation):
#   - threshold 0.45 flags 104/358 (~29%) — far too many false positives
#   - threshold 1.00 flags ~30/358 (~8%) — matches realistic foil rate
#   - threshold 1.50 flags ~15/358 (~4%) — misses scan 205 (conf=1.39)
#
# 1.0 is the conservative first-cut default: catches the known foil with
# margin, flags roughly the expected foil rate in a casual collection.
# Re-tune after hand-labeling foil_multisignal/top30/*.
FOIL_CONFIDENCE_THRESHOLD = 1.0


def _compute_bright_stats(img_bgr):
    """
    Compute bright-pixel saturation + hue statistics for a single image.

    :param img_bgr: BGR uint8 numpy array
    :returns: dict with:
        bright_frac, n_bright, mean_s_bright, hue_range_bright
        If too few bright pixels, mean_s_bright and hue_range_bright are None.
    """
    if img_bgr is None or img_bgr.size == 0:
        return None

    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)

    total = img_bgr.shape[0] * img_bgr.shape[1]
    bright_mask = v > BRIGHT_V_THRESH
    n_bright = int(bright_mask.sum())
    bright_frac = n_bright / float(total) if total else 0.0

    if n_bright < MIN_BRIGHT_PIXELS:
        return {
            "n_bright": n_bright,
            "bright_frac": bright_frac,
            "mean_s_bright": None,
            "hue_range_bright": None,
        }

    s_bright = s[bright_mask]
    h_bright = h[bright_mask]

    mean_s = float(s_bright.mean())
    # Hue is 0-179 in OpenCV HSV. Spread = p90 - p10 to ignore outliers.
    # For a narrow-hue cluster (non-foil), spread is small.
    # For a rainbow scatter (foil), spread approaches 90+.
    hue_range = float(np.percentile(h_bright, 90) - np.percentile(h_bright, 10))

    return {
        "n_bright": n_bright,
        "bright_frac": bright_frac,
        "mean_s_bright": mean_s,
        "hue_range_bright": hue_range,
    }


def _resize_to_match(img_a, img_b):
    """Resize img_a to match img_b's dimensions. Returns resized img_a."""
    h, w = img_b.shape[:2]
    if img_a.shape[:2] == (h, w):
        return img_a
    return cv2.resize(img_a, (w, h), interpolation=cv2.INTER_AREA)


def _load_reference(card_id):
    """Load the reference PNG for a card ID. Returns BGR image or None."""
    if not card_id:
        return None
    path = os.path.join(REFERENCE_DIR, f"{card_id}.png")
    if not os.path.isfile(path):
        return None
    img = cv2.imread(path)
    return img


def detect_foil(card_img, card_id: Optional[str] = None,
                reference_img=None,
                threshold: float = FOIL_CONFIDENCE_THRESHOLD) -> dict:
    """
    Detect whether a scanned card is a foil printing.

    :param card_img:      Oriented BGR card image (post-warp, post-rotation).
                          Expected to be 745x1040 but any size works.
    :param card_id:       Scryfall UUID; used to load reference image.
                          Ignored if reference_img is provided.
    :param reference_img: Pre-loaded BGR reference image (optional).
    :param threshold:     Confidence threshold above which is_foil=True.

    :returns: dict with:
        is_foil:          bool
        confidence:       float in ~[-1, +1], higher = more foil-like
        reason:           short string (e.g. "insufficient_bright",
                          "no_reference", "low_confidence")
        signals:          dict of computed signal values (for diagnostics)
    """
    out = {
        "is_foil": False,
        "confidence": 0.0,
        "reason": "unknown",
        "signals": {},
    }

    if card_img is None or card_img.size == 0:
        out["reason"] = "invalid_image"
        return out

    # --- 1. Compute scan-side signals ---
    scan_stats = _compute_bright_stats(card_img)
    if scan_stats is None:
        out["reason"] = "invalid_image"
        return out

    out["signals"]["scan"] = scan_stats

    if scan_stats["n_bright"] < MIN_BRIGHT_PIXELS:
        # Very dark card — no bright pixels to analyze. Foil detection
        # needs specular reflection to work, so treat as nonfoil.
        out["reason"] = "insufficient_bright"
        return out

    # --- 2. Load or use reference ---
    ref_img = reference_img
    if ref_img is None and card_id:
        ref_img = _load_reference(card_id)

    if ref_img is None:
        # Without a reference we can only use absolute signals, which we
        # already know don't discriminate well. Return not-foil with low
        # confidence so downstream treats as unknown.
        out["reason"] = "no_reference"
        return out

    ref_resized = _resize_to_match(ref_img, card_img)
    ref_stats = _compute_bright_stats(ref_resized)
    if ref_stats is None:
        out["reason"] = "invalid_reference"
        return out

    out["signals"]["reference"] = ref_stats

    # --- 3. Compute deltas ---
    delta_bright_frac = (scan_stats["bright_frac"]
                         - ref_stats["bright_frac"])

    # mean_s may be None in either frame if that frame had too few bright
    # pixels. Handle gracefully.
    scan_ms = scan_stats["mean_s_bright"]
    ref_ms = ref_stats["mean_s_bright"]
    if scan_ms is not None and ref_ms is not None:
        delta_mean_s = scan_ms - ref_ms
    else:
        delta_mean_s = 0.0

    # Hue range is already a scan-only signal — foils scatter regardless of
    # reference. But subtracting the reference's own hue range corrects for
    # naturally colorful art.
    scan_hr = scan_stats["hue_range_bright"] or 0.0
    ref_hr = ref_stats["hue_range_bright"] or 0.0
    delta_hue_range = scan_hr - ref_hr

    out["signals"]["delta_bright_frac"] = delta_bright_frac
    out["signals"]["delta_mean_s"] = delta_mean_s
    out["signals"]["delta_hue_range"] = delta_hue_range

    # --- 4. Score ---
    # Foil indicators:
    #   delta_bright_frac > 0      (more highlights in scan)
    #   delta_mean_s      < 0      (hotspots desaturate toward white)
    #   delta_hue_range   > 0      (rainbow scatter in bright pixels)
    confidence = (
        W_DELTA_BRIGHT_FRAC * delta_bright_frac
        + W_DELTA_MEAN_S   * delta_mean_s
        + W_HUE_RANGE_BRIGHT * delta_hue_range
    )

    out["confidence"] = float(confidence)
    out["is_foil"] = confidence >= threshold
    out["reason"] = "ok"
    return out


# ---------------------------------------------------------------------------
# CLI entry for smoke-testing
# ---------------------------------------------------------------------------

def _smoke_test():
    """Run detection against the known reference foil if available."""
    ref_foil = os.path.join(SCRIPT_DIR, "foil_signal1",
                            "REFERENCE_FOIL_scan0205_s051.2.jpg")
    if not os.path.isfile(ref_foil):
        print("Known-foil reference image not found; skipping smoke test.")
        return

    img = cv2.imread(ref_foil)
    # scan 205 = Zombie Infestation | set=gpt | num=55
    # Need to look up the card_id from cards.py
    try:
        from cards import CARDS_DATA
    except Exception:
        CARDS_DATA = None

    # scan 205 matched set=gpt in the test log, but the actual scanned
    # card was Zombie Infestation — which was never printed in Guildpact.
    # The CSV set code is the mismatched hash output. Use the first
    # Zombie Infestation printing we can find a reference PNG for.
    card_id = None
    if CARDS_DATA:
        for c in CARDS_DATA:
            if c.get("name", "").lower() == "zombie infestation":
                cid = c.get("id")
                if cid and os.path.isfile(os.path.join(
                        REFERENCE_DIR, f"{cid}.png")):
                    card_id = cid
                    break

    result = detect_foil(img, card_id=card_id)
    print(f"Known foil (scan 205, Zombie Infestation): "
          f"card_id={card_id}")
    print(f"  -> {result}")


if __name__ == "__main__":
    _smoke_test()
