# foil_detect.py
# ---------------------------------------------------------------------------
# Post-identification foil detection for scanned cards.
#
# Runs AFTER card_identify has matched a scan to a card ID. Uses the matched
# card's reference PNG (downloaded_cards/<card_id>.png) to distinguish "this
# specific scan is a foil printing" from "this card has bright saturated art".
#
# The approach: compare the scan's bright-pixel statistics to the Scryfall
# reference image. Under the current LED scanner:
#   - Non-foil cards diffuse-reflect. Under the bright LED the scan comes
#     out UNIFORMLY BRIGHTER than the matte Scryfall reference, with
#     bright regions that roughly match the reference's saturation.
#   - Foil cards reflect most light off-angle (away from the camera). The
#     overall scan reads DARKER or ~equal to the reference, but the
#     occasional specular hotspot appears as a saturated rainbow color.
#
# No single signal alone works — many non-foil cards (basic lands, full-art
# promos) have naturally bright saturated art and would trip a single-
# threshold detector. The reference-relative deltas correct for that.
#
# Five signals combine into a single confidence score (Level-2 model,
# 2026-04-23 retune). Signs and magnitudes shown for the new-lighting
# directional LED setup.
#
#   S1: delta_bright_frac = bright_frac(scan) - bright_frac(ref)
#       Foils are DARKER or equal vs their reference under LED scan
#       (most light reflects off-angle). Non-foils are brighter (diffuse).
#       Strongest signal — Cohen's d ~ -1.7, single-feature AUC 0.90.
#
#   S2: delta_mean_s = mean_s_bright(scan) - mean_s_bright(ref)
#       Foil bright pixels are SATURATED rainbow hotspots (one hue per
#       spot), while non-foil bright pixels match the reference's
#       desaturated white/light regions.
#       Cohen's d ~ +1.3, single-feature AUC 0.84.
#
#   S3: delta_n_bright_clusters = #connected-components in bright mask
#       Foils have FEWER large bright clusters than ref because off-angle
#       reflection drops many would-be bright pixels below the V=220
#       threshold. Cohen's d ~ -0.4, single-feature AUC 0.65.
#
#   S4: delta_std_s_bright = std of saturation among bright pixels
#       After dms accounts for the saturation level, residual variance
#       turns out to be slightly LOWER in foil scans than in their refs
#       relative to nonfoils — opposite the univariate intuition (this is
#       multicollinearity with dms). AUC 0.67.
#
#   S5: delta_laplacian_energy = mean |Laplacian(V)| within bright mask
#       Bright zones in foil scans preserve more high-frequency content
#       (specular edge transitions) than diffuse-bright nonfoil zones do.
#       Weakest signal — coefficient ~ -0.003. AUC 0.67.
#
#   delta_hue_range is computed for diagnostics but NOT scored. Its
#   coefficient collapsed to ~0 in the multi-feature fit.
#
# Public API:
#   detect_foil(card_img, card_id=None, reference_img=None) -> dict
#
# Tuning note: default threshold tuned for 91% precision / 74% recall on
# the 2026-04-23 labeled set (445 samples). Re-tune via _foil_tune.py
# when lighting or camera changes significantly.
# ---------------------------------------------------------------------------

import json
import os
from typing import Optional

import cv2
import numpy as np

from config import PRINTINGS_MAP_PATH, SCRIPT_DIR

# --- Paths ---
REFERENCE_DIR = os.path.join(SCRIPT_DIR, "downloaded_cards")

# --- Printing → representative map (lazy) ---
# The download pipeline dedupes by (illustration_id, frame), so many
# printings share a single PNG file stored under a representative
# card_id, NOT under their own card_id. This inverse map lets
# _load_reference() find the PNG even when a direct {card_id}.png
# lookup fails. Built on first call from printings_map.json.
_PRINTING_TO_REP: Optional[dict] = None

# --- Bright-pixel gate ---
BRIGHT_V_THRESH = 220          # HSV V above this = "bright pixel"
MIN_BRIGHT_PIXELS = 500        # need this many to trust the signal

# --- Specular cluster filter ---
# After bright-mask morph-close, only count connected components of at least
# this many pixels as a "cluster". Suppresses single-pixel JPEG noise and
# isolated speckles. 25 = ~5x5 region.
MIN_CLUSTER_PIXELS = 25

# --- Score weights ---
#
# Tuned 2026-04-23 via logistic regression on:
#   - Session 51 (58 confirmed foils, new lighting)
#   - Session 44 (387 casual scans, 12 foils relabeled after manual review)
# Total training set: 70 foils + 375 nonfoils = 445 labeled samples.
#
# This is the Level-2 multi-signal upgrade. Two new structural bright-mask
# signals were added on top of the original (dbf, dms) pair, and the
# noise-floor `delta_hue_range` signal was dropped after the multi-feature
# fit confirmed its coefficient was effectively zero (|w * range| < 0.2).
#
# All weight signs follow the new-lighting physics:
#   - Foils reflect most light off-angle, so scan appears DARKER than the
#     Scryfall ref (dbf < 0) where nonfoils read BRIGHTER (dbf > 0).
#   - Foil bright pixels are saturated rainbow hotspots (dms > 0).
#   - Foils have FEWER large bright clusters than ref (dnc < 0), because
#     off-angle reflection drops many would-be bright pixels below the
#     V=220 threshold.
#   - Foils' bright pixels span a narrower saturation range vs ref than
#     nonfoils do (dss < 0 in this multi-feature regime, after dms accounts
#     for the saturation level itself — multicollinearity flips the sign
#     vs the univariate AUC analysis).
#   - Foils preserve more high-frequency content within bright zones than
#     nonfoils do vs their reference renders (dle weight is small but real).
#
# See plans/handoff/ (foil retune notes) and _foil_tune.py for the fit.
W_DELTA_BRIGHT_FRAC       = -22.7420   # was -18.92
W_DELTA_MEAN_S            = +0.08221   # was +0.0474
W_DELTA_N_BRIGHT_CLUSTERS = -0.01683   # NEW (Level-2)
W_DELTA_STD_S_BRIGHT      = -0.02960   # NEW (Level-2)
W_DELTA_LAPLACIAN_ENERGY  = -0.00347   # NEW (Level-2)
FOIL_BIAS                 = -0.1815    # was +0.294
# delta_hue_range is no longer scored — see _compute_bright_stats; it's
# still computed for diagnostics but contributes 0 to the confidence.

# --- Classification threshold ---
# confidence >= this -> is_foil = True
#
# Calibration on 70 foils + 375 nonfoils (2026-04-23 Level-2 retune):
#   - +0.75 -> 87% precision, 86% recall (9 FP / 60 TP)  <- best F1 = 0.863
#   - +1.25 -> 90% precision, 79% recall (6 FP / 55 TP)
#   - +1.75 -> 91% precision, 74% recall (5 FP / 52 TP)  <-- default
#   - +2.25 -> 94% precision, 69% recall (3 FP / 48 TP)
#
# +1.75 picked as default: keeps precision ≥91% (matching the user's
# prior comfort bar) while extracting ~6 points more recall than the
# shipped 3-feature model at the same precision target.
FOIL_CONFIDENCE_THRESHOLD = 1.75


def _compute_bright_stats(img_bgr):
    """
    Compute bright-pixel statistics for a single image.

    Includes the original 4 fields used by the scoring formula plus 3
    Level-2 diagnostic fields (n_bright_clusters, std_s_bright,
    laplacian_energy_bright) that are NOT yet in the scoring formula —
    these are surfaced for instrumentation while we evaluate whether
    they separate foils from nonfoils on real data.

    :param img_bgr: BGR uint8 numpy array
    :returns: dict with:
        bright_frac, n_bright, mean_s_bright, hue_range_bright,
        n_bright_clusters, std_s_bright, laplacian_energy_bright
        If too few bright pixels, the post-mask fields are None.
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
            "n_bright_clusters": None,
            "std_s_bright": None,
            "laplacian_energy_bright": None,
        }

    s_bright = s[bright_mask]
    h_bright = h[bright_mask]

    mean_s = float(s_bright.mean())
    # Hue is 0-179 in OpenCV HSV. Spread = p90 - p10 to ignore outliers.
    # For a narrow-hue cluster (non-foil), spread is small.
    # For a rainbow scatter (foil), spread approaches 90+.
    hue_range = float(np.percentile(h_bright, 90) - np.percentile(h_bright, 10))

    # --- Level-2 diagnostic signals (instrumentation, not scored) ---

    # std_s_bright: spread of saturation among bright pixels.
    # Foils: rainbow hotspots span saturation values -> high std.
    # Diffuse-bright (sky, paper-white): uniform saturation -> low std.
    std_s = float(s_bright.std())

    # n_bright_clusters: count of distinct specular hotspots after
    # noise-suppression close. Foils: many small hotspots (-> high count).
    # Full-art bright skies / matte-bright cards: one or two large blobs
    # (-> low count). This is the signal that should specifically rescue
    # the basic-land foil failure mode.
    mask_u8 = bright_mask.astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask_closed = cv2.morphologyEx(mask_u8, cv2.MORPH_CLOSE, kernel)
    num_components, labels = cv2.connectedComponents(mask_closed)
    if num_components > 1:
        # bincount[0] is background (label 0); skip it
        sizes = np.bincount(labels.flatten())[1:]
        n_clusters = int((sizes >= MIN_CLUSTER_PIXELS).sum())
    else:
        n_clusters = 0

    # laplacian_energy_bright: mean |Laplacian(V)| over bright pixels.
    # Specular reflections have sharp intensity transitions; diffuse
    # bright regions are smooth. Foils -> higher Laplacian energy.
    lap = cv2.Laplacian(v, cv2.CV_32F, ksize=3)
    laplacian_energy = float(np.abs(lap)[bright_mask].mean())

    return {
        "n_bright": n_bright,
        "bright_frac": bright_frac,
        "mean_s_bright": mean_s,
        "hue_range_bright": hue_range,
        "n_bright_clusters": n_clusters,
        "std_s_bright": std_s,
        "laplacian_energy_bright": laplacian_energy,
    }


def _resize_to_match(img_a, img_b):
    """Resize img_a to match img_b's dimensions. Returns resized img_a."""
    h, w = img_b.shape[:2]
    if img_a.shape[:2] == (h, w):
        return img_a
    return cv2.resize(img_a, (w, h), interpolation=cv2.INTER_AREA)


def _build_printing_to_rep() -> dict:
    """Load printings_map.json once and build the inverse map
    {printing_id -> representative_id}.

    Returns {} if the map file is missing or unreadable (the caller
    will then just fail the no_reference check as before).
    """
    global _PRINTING_TO_REP
    if _PRINTING_TO_REP is not None:
        return _PRINTING_TO_REP

    inv = {}
    if os.path.isfile(PRINTINGS_MAP_PATH):
        try:
            with open(PRINTINGS_MAP_PATH, 'r', encoding='utf-8') as f:
                pm = json.load(f)
            for rep_id, entry in pm.items():
                for printing in entry.get('printings', []):
                    pid = printing.get('id')
                    if pid:
                        inv[pid] = rep_id
        except (OSError, ValueError):
            pass  # leave inv empty; fallback will just no-op

    _PRINTING_TO_REP = inv
    return inv


def _load_reference(card_id):
    """Load the reference PNG for a card ID. Returns BGR image or None.

    Tries `{card_id}.png` directly first. If that fails, falls back via
    printings_map.json — the download pipeline dedupes by
    (illustration_id, frame), so many printings share a single PNG
    stored under a representative card_id.
    """
    if not card_id:
        return None

    # Direct lookup
    path = os.path.join(REFERENCE_DIR, f"{card_id}.png")
    if os.path.isfile(path):
        return cv2.imread(path)

    # Fallback: this card's printing was deduped to a representative
    inv = _build_printing_to_rep()
    rep_id = inv.get(card_id)
    if rep_id and rep_id != card_id:
        rep_path = os.path.join(REFERENCE_DIR, f"{rep_id}.png")
        if os.path.isfile(rep_path):
            return cv2.imread(rep_path)

    return None


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

    # --- 3b. Level-2 deltas (now scored) ---
    def _delta(field):
        a = scan_stats.get(field)
        b = ref_stats.get(field)
        if a is None or b is None:
            return 0.0
        return float(a - b)

    delta_n_bright_clusters = _delta("n_bright_clusters")
    delta_std_s_bright = _delta("std_s_bright")
    delta_laplacian_energy = _delta("laplacian_energy_bright")
    out["signals"]["delta_n_bright_clusters"] = delta_n_bright_clusters
    out["signals"]["delta_std_s_bright"] = delta_std_s_bright
    out["signals"]["delta_laplacian_energy"] = delta_laplacian_energy

    # --- 4. Score (5-feature Level-2 model) ---
    # Sign of each contribution under new-lighting physics — see W_*
    # definitions for the full reasoning:
    #   delta_bright_frac        < 0  (foils darker than ref)
    #   delta_mean_s             > 0  (foil bright pixels saturated)
    #   delta_n_bright_clusters  < 0  (foils have fewer surviving clusters)
    #   delta_std_s_bright       < 0  (residual after dms accounts for level)
    #   delta_laplacian_energy   < 0  (residual high-freq, weakest signal)
    # delta_hue_range is computed for diagnostics but no longer scored
    # (its weight collapsed to ~0 in the multi-feature fit).
    confidence = (
        FOIL_BIAS
        + W_DELTA_BRIGHT_FRAC       * delta_bright_frac
        + W_DELTA_MEAN_S            * delta_mean_s
        + W_DELTA_N_BRIGHT_CLUSTERS * delta_n_bright_clusters
        + W_DELTA_STD_S_BRIGHT      * delta_std_s_bright
        + W_DELTA_LAPLACIAN_ENERGY  * delta_laplacian_energy
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
