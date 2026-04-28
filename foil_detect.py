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
# Transform / MDFC back-face handling:
#   For cards with a `{card_id}__back.png` reference (battles, sieges,
#   transforms), the scan might be of the front or the back. detect_foil
#   picks the matching face via perceptual-hash distance so the brightness
#   deltas compare scan-vs-same-face rather than scan-vs-wrong-art (which
#   masquerades as a foil signal). See _load_reference_back().
#
# Public API:
#   detect_foil(card_img, card_id=None, reference_img=None) -> dict
#
# Tuning note: default threshold tuned for 96% precision / 71% recall on
# the 2026-04-23 labeled set (445 samples) with DFC face-picking in place.
# Re-tune via _foil_tune.py when lighting or camera changes significantly.
# ---------------------------------------------------------------------------

import glob
import json
import os
import time
import urllib.request
import urllib.error
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

# --- card_id → image URL index (lazy, built from newest bulk JSON) ---
_IMAGE_URL_INDEX: Optional[dict] = None

def _build_image_url_index() -> dict:
    global _IMAGE_URL_INDEX
    if _IMAGE_URL_INDEX is not None:
        return _IMAGE_URL_INDEX
    pattern = os.path.join(SCRIPT_DIR, "default-cards-*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        _IMAGE_URL_INDEX = {}
        return _IMAGE_URL_INDEX
    with open(files[-1], "r", encoding="utf-8") as f:
        cards = json.load(f)
    idx = {}
    for c in cards:
        cid = c.get("id")
        if not cid:
            continue
        uris = c.get("image_uris") or {}
        url = uris.get("png") or uris.get("normal")
        if not url:
            faces = c.get("card_faces") or []
            if faces:
                furis = faces[0].get("image_uris") or {}
                url = furis.get("png") or furis.get("normal")
        if url:
            idx[cid] = url
    _IMAGE_URL_INDEX = idx
    return _IMAGE_URL_INDEX


def _fetch_reference(card_id: str) -> Optional[str]:
    """Download the reference PNG for card_id if it's missing. Returns path or None."""
    dest = os.path.join(REFERENCE_DIR, f"{card_id}.png")
    if os.path.isfile(dest):
        return dest
    url_idx = _build_image_url_index()
    url = url_idx.get(card_id)
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "CardSorter/1.0 (on-demand ref fetch)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        with open(dest, "wb") as f:
            f.write(data)
        return dest
    except (urllib.error.URLError, OSError):
        return None

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
#
# Level-4 retune 2026-04-27: added 131 per-scan verified verdicts from
# session 58 (926-card mixed session, borderline |conf| < 0.5 zone reviewed
# via foil_review page). Training set now 120 foils + 471 nonfoils.
# The new borderline samples shifted the bias term significantly (was -0.09,
# now -0.54) and tightened the threshold from +1.00 to +0.75 for best F1.
# At 95% precision: recall 76.67% (was 71.67%), F1 0.855 (was 0.822).
W_DELTA_BRIGHT_FRAC       = -20.6193   # was -21.5507 (Level-3)
W_DELTA_MEAN_S            = +0.08017   # was +0.08301
W_DELTA_N_BRIGHT_CLUSTERS = -0.01779   # was -0.01780
W_DELTA_STD_S_BRIGHT      = -0.02388   # was -0.03178
W_DELTA_LAPLACIAN_ENERGY  = -0.00585   # was -0.00392
FOIL_BIAS                 = -0.5425    # was -0.0949
# delta_hue_range is no longer scored — see _compute_bright_stats; it's
# still computed for diagnostics but contributes 0 to the confidence.

# --- Classification threshold ---
# confidence >= this -> is_foil = True
#
# Calibration on 120 foils + 471 nonfoils (2026-04-27 Level-4 retune —
# 131 per-scan borderline verdicts from session 58 added):
#   - +0.50 -> 90.65% precision, 80.83% recall (10 FP / 97 TP)
#   - +0.75 -> 95.83% precision, 76.67% recall (4 FP / 92 TP)  <-- default
#   - +1.00 -> 96.67% precision, 72.50% recall (3 FP / 87 TP)
#
# +0.75 adopted as default: best-F1 (0.855) sits here. Improves recall
# +5pp vs Level-3 default (+1.00) with comparable precision (95.8% vs 95.6%).
FOIL_CONFIDENCE_THRESHOLD = 0.75


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

    # Last resort: download on demand
    fetched = _fetch_reference(card_id)
    if fetched:
        return cv2.imread(fetched)

    return None


def _load_reference_back(card_id):
    """Load the BACK-FACE reference PNG for a transform/MDFC card.

    Returns BGR image or None if no back face exists. Mirrors
    _load_reference()'s resolution order:
      1. `{card_id}__back.png`
      2. `{rep_id}__back.png` via printings_map fallback

    Exists so detect_foil() can auto-correct when the scanner captures
    the creature/back side of a transform card but the identifier's
    canonical PNG is the horizontally-oriented front (battle, siege,
    etc.). Comparing against a totally different face's art produces
    garbage brightness deltas and mimics the foil signature.
    """
    if not card_id:
        return None

    path = os.path.join(REFERENCE_DIR, f"{card_id}__back.png")
    if os.path.isfile(path):
        return cv2.imread(path)

    inv = _build_printing_to_rep()
    rep_id = inv.get(card_id)
    if rep_id and rep_id != card_id:
        rep_path = os.path.join(REFERENCE_DIR, f"{rep_id}__back.png")
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

    # --- 2b. Transform-DFC back-face check ---
    # When the card is a transform/MDFC (battle, siege, etc.), {card_id}.png
    # is the front face but the scanner may have captured the back. The
    # front face (e.g. horizontal battle art) is completely different from
    # the back face (vertical creature), which blows up the brightness
    # deltas and produces spurious high foil confidence. If a __back.png
    # exists, compare scan perceptual-hash distance to both refs and pick
    # the one that actually matches what was scanned.
    #
    # Why phash rather than bright_frac: the LED scanner brightens the
    # scan non-uniformly vs the Scryfall renders, so scan_bf isn't
    # reliably close to the matching ref's bf — especially when the back
    # ref is stored at lower resolution (some cards). phash is
    # resolution- and brightness-invariant and captures structural
    # similarity. Empirically: all 3 MOM battle FPs in session 44
    # (scans 6, 8, 14) picked back correctly via phash; scan 14 flipped
    # to front under the pure bright_frac heuristic.
    #
    # Only runs when the caller didn't hand us a specific reference_img —
    # that path explicitly opts out of face-picking.
    matched_face = "front"
    if reference_img is None and card_id:
        ref_back_img = _load_reference_back(card_id)
        if ref_back_img is not None:
            # Lazy-import imagehash/PIL so cards without __back.png never
            # pay the import cost.
            try:
                from PIL import Image
                import imagehash
                scan_rgb = cv2.cvtColor(card_img, cv2.COLOR_BGR2RGB)
                front_rgb = cv2.cvtColor(ref_img, cv2.COLOR_BGR2RGB)
                back_rgb = cv2.cvtColor(ref_back_img, cv2.COLOR_BGR2RGB)
                scan_h = imagehash.phash(Image.fromarray(scan_rgb))
                front_h = imagehash.phash(Image.fromarray(front_rgb))
                back_h = imagehash.phash(Image.fromarray(back_rgb))
                dist_front = scan_h - front_h
                dist_back = scan_h - back_h
                if dist_back < dist_front:
                    ref_back_resized = _resize_to_match(ref_back_img,
                                                        card_img)
                    back_stats = _compute_bright_stats(ref_back_resized)
                    if back_stats is not None:
                        ref_img = ref_back_img
                        ref_resized = ref_back_resized
                        ref_stats = back_stats
                        matched_face = "back"
            except ImportError:
                # imagehash/PIL not available — silently keep front ref.
                # This preserves the pre-fix behavior as a safe fallback.
                pass

    out["signals"]["reference"] = ref_stats
    out["signals"]["matched_face"] = matched_face

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
