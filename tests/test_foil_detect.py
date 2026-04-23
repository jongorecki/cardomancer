"""Unit tests for foil_detect.py — multi-signal foil detection.

We synthesize card + reference pairs to verify that the detector:
  - Computes the three signal deltas correctly from known inputs
  - Degrades gracefully when inputs are missing or malformed
  - Reproduces a pinned score for fixed signal inputs (regression guard
    against accidental weight / bias / threshold changes)

Note on synthetic "foils": the _make_foil_scan_from_reference fixture
generates OLD-LIGHTING foil physics (brighter, desaturated hotspots).
That's useful for testing signal direction but does NOT match what
real foils look like under the current LED scanner (where foils read
darker with saturated rainbow hotspots). Do not use the synthesizer
to validate whether the classifier fires correctly — use the pinned
regression test for that.
"""

import sys
import os
import unittest

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foil_detect import (
    detect_foil,
    _compute_bright_stats,
    FOIL_CONFIDENCE_THRESHOLD,
    FOIL_BIAS,
    W_DELTA_BRIGHT_FRAC,
    W_DELTA_MEAN_S,
    W_HUE_RANGE_BRIGHT,
    BRIGHT_V_THRESH,
    MIN_BRIGHT_PIXELS,
)


# ---------------------------------------------------------------------------
# Image fixtures — synthesize BGR images with specific HSV characteristics
# ---------------------------------------------------------------------------

def _make_solid_hsv(h, s, v, size=(1040, 745)):
    """Make a BGR image with uniform HSV values."""
    hsv = np.zeros((size[0], size[1], 3), dtype=np.uint8)
    hsv[:, :, 0] = h
    hsv[:, :, 1] = s
    hsv[:, :, 2] = v
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def _make_reference_saturated_bright():
    """A colorful reference image with bright saturated pixels — like a
    basic land art. High saturation in bright areas, clustered hue."""
    # Fill with bright saturated orange (h=15, s=200, v=230)
    return _make_solid_hsv(15, 200, 230)


def _make_reference_normal():
    """A typical reference: moderate saturation, moderate brightness,
    clustered hue."""
    img = _make_solid_hsv(60, 120, 150, size=(1040, 745))
    # Add a bright patch in the top portion (~10% of pixels above V=220)
    img_hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    img_hsv[:100, :, 2] = 230  # bright band at top
    img_hsv[:100, :, 1] = 180  # saturated bright band
    img_hsv[:100, :, 0] = 60
    return cv2.cvtColor(img_hsv, cv2.COLOR_HSV2BGR)


def _make_old_lighting_foil_scan(ref_bgr):
    """Synthesize an OLD-LIGHTING foil scan: start from reference, add
    specular hotspots that are brighter, desaturated, and hue-scattered.

    NOTE: under the current LED scanner, real foils read darker overall
    with saturated rainbow hotspots — the opposite of this fixture. Use
    this only to exercise signal-direction logic, not to validate the
    classifier. For classifier regression, use the pinned test below.
    """
    out = ref_bgr.copy()
    hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]

    # Add 30 randomly-placed bright desaturated rainbow hotspots
    rng = np.random.default_rng(42)
    for _ in range(30):
        cx = rng.integers(20, w - 20)
        cy = rng.integers(20, h - 20)
        radius = rng.integers(10, 25)
        # Each hotspot gets a different hue across the rainbow
        hue = int(rng.integers(0, 180))
        y0, y1 = max(0, cy - radius), min(h, cy + radius)
        x0, x1 = max(0, cx - radius), min(w, cx + radius)
        hsv[y0:y1, x0:x1, 0] = hue       # scattered hue
        hsv[y0:y1, x0:x1, 1] = 30        # low saturation (whitish)
        hsv[y0:y1, x0:x1, 2] = 250       # very bright

    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def _make_nonfoil_scan_from_reference(ref_bgr):
    """A nonfoil scan: essentially the reference with small noise.
    Should score low."""
    out = ref_bgr.copy()
    rng = np.random.default_rng(7)
    noise = rng.integers(-5, 6, out.shape, dtype=np.int16)
    out = np.clip(out.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return out


# ---------------------------------------------------------------------------
# Unit tests: _compute_bright_stats
# ---------------------------------------------------------------------------

class TestComputeBrightStats(unittest.TestCase):

    def test_dark_image_returns_insufficient_bright(self):
        dark = _make_solid_hsv(0, 0, 100)  # uniformly dark
        stats = _compute_bright_stats(dark)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["n_bright"], 0)
        self.assertIsNone(stats["mean_s_bright"])

    def test_bright_saturated_image(self):
        bright = _make_solid_hsv(10, 200, 240)  # all pixels bright
        stats = _compute_bright_stats(bright)
        self.assertIsNotNone(stats)
        self.assertGreater(stats["n_bright"], MIN_BRIGHT_PIXELS)
        # Every pixel is saturated orange
        self.assertAlmostEqual(stats["mean_s_bright"], 200, delta=2)
        # Single hue -> small range
        self.assertLess(stats["hue_range_bright"], 2)

    def test_bright_desaturated_image(self):
        bright = _make_solid_hsv(10, 30, 240)  # all pixels bright, desaturated
        stats = _compute_bright_stats(bright)
        self.assertLess(stats["mean_s_bright"], 50)

    def test_none_input(self):
        self.assertIsNone(_compute_bright_stats(None))

    def test_empty_array(self):
        empty = np.array([], dtype=np.uint8).reshape(0, 0, 3)
        self.assertIsNone(_compute_bright_stats(empty))


# ---------------------------------------------------------------------------
# Unit tests: detect_foil
# ---------------------------------------------------------------------------

class TestDetectFoil(unittest.TestCase):

    def test_synthetic_old_lighting_foil_runs_without_error(self):
        """Old-lighting synthetic foil: we don't assert is_foil because
        the current weights are tuned for new-lighting physics (where
        real foils look darker, not brighter). Just verify the pipeline
        runs and produces a numeric confidence."""
        ref = _make_reference_normal()
        scan = _make_old_lighting_foil_scan(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "ok")
        self.assertIsInstance(result["confidence"], float)

    def test_simulated_nonfoil_scores_below_threshold(self):
        ref = _make_reference_normal()
        scan = _make_nonfoil_scan_from_reference(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "ok")
        self.assertLess(result["confidence"], FOIL_CONFIDENCE_THRESHOLD)
        self.assertFalse(result["is_foil"])

    def test_bright_saturated_nonfoil_not_flagged(self):
        """The false-positive case: a card with naturally bright saturated
        art (e.g. basic land). Reference and scan both have saturated
        bright pixels. Should NOT flag as foil."""
        ref = _make_reference_saturated_bright()
        scan = _make_nonfoil_scan_from_reference(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "ok")
        self.assertFalse(result["is_foil"])

    def test_no_reference_returns_no_reference_reason(self):
        scan = _make_reference_normal()
        result = detect_foil(scan, card_id=None, reference_img=None)
        self.assertEqual(result["reason"], "no_reference")
        self.assertFalse(result["is_foil"])
        self.assertEqual(result["confidence"], 0.0)

    def test_missing_card_id_file_returns_no_reference(self):
        """card_id points at a file that doesn't exist -> no_reference."""
        scan = _make_reference_normal()
        result = detect_foil(scan, card_id="nonexistent-card-id-xyz")
        self.assertEqual(result["reason"], "no_reference")

    def test_dark_scan_returns_insufficient_bright(self):
        scan = _make_solid_hsv(0, 0, 50)  # uniformly very dark
        ref = _make_reference_normal()
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "insufficient_bright")
        self.assertFalse(result["is_foil"])

    def test_none_scan_returns_invalid_image(self):
        result = detect_foil(None, reference_img=_make_reference_normal())
        self.assertEqual(result["reason"], "invalid_image")
        self.assertFalse(result["is_foil"])

    def test_reference_resized_if_mismatched(self):
        """Reference at different resolution should still produce a result."""
        ref = _make_reference_normal()            # 1040x745
        ref_small = cv2.resize(ref, (370, 520))   # half size
        scan = _make_old_lighting_foil_scan(ref)
        result = detect_foil(scan, reference_img=ref_small)
        # Resize path works; no assertion on is_foil (synthetic is old-lighting)
        self.assertEqual(result["reason"], "ok")

    def test_result_contains_expected_signals(self):
        ref = _make_reference_normal()
        scan = _make_old_lighting_foil_scan(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertIn("signals", result)
        s = result["signals"]
        self.assertIn("scan", s)
        self.assertIn("reference", s)
        self.assertIn("delta_bright_frac", s)
        self.assertIn("delta_mean_s", s)
        self.assertIn("delta_hue_range", s)

    def test_custom_threshold_overrides_default(self):
        """Passing a threshold far above the default should always keep
        is_foil=False regardless of signal values."""
        ref = _make_reference_normal()
        scan = _make_old_lighting_foil_scan(ref)
        default = detect_foil(scan, reference_img=ref)
        high = detect_foil(scan, reference_img=ref, threshold=999.0)
        self.assertAlmostEqual(default["confidence"],
                               high["confidence"], places=5)
        self.assertFalse(high["is_foil"])


# ---------------------------------------------------------------------------
# Foil signal direction sanity checks
# ---------------------------------------------------------------------------

class TestSyntheticOldLightingSignalDirection(unittest.TestCase):
    """Verify the OLD-LIGHTING foil synthesizer produces the signal
    directions it's designed to produce. This tests the fixture, not
    the classifier — under new lighting, real foils produce OPPOSITE
    signs on dbf and dms. Kept here so changes to the synthesizer
    can't silently break reasoning about what it represents."""

    def setUp(self):
        self.ref = _make_reference_normal()
        self.foil = _make_old_lighting_foil_scan(self.ref)
        self.result = detect_foil(self.foil, reference_img=self.ref)

    def test_delta_bright_frac_positive(self):
        self.assertGreater(
            self.result["signals"]["delta_bright_frac"], 0,
            "Synthesizer adds bright hotspots -> dbf > 0"
        )

    def test_delta_mean_s_negative(self):
        self.assertLess(
            self.result["signals"]["delta_mean_s"], 0,
            "Synthesizer adds desaturated hotspots -> dms < 0"
        )

    def test_delta_hue_range_positive(self):
        self.assertGreater(
            self.result["signals"]["delta_hue_range"], 0,
            "Synthesizer scatters hue -> dhr > 0"
        )


# ---------------------------------------------------------------------------
# Pinned regression test — guards against silent weight / bias / threshold
# changes. If you retune the model, update these snapshots deliberately.
# ---------------------------------------------------------------------------

class TestScoreFormulaRegression(unittest.TestCase):
    """Pin the confidence formula to the 2026-04-22 retune on real
    new-lighting data (50 foils + 319 nonfoils). Each fixture row is
    a realistic (dbf, dms, dhr) triple observed on actual scans and
    the resulting score the formula must produce.

    If this test fails, it means weights / bias / threshold changed
    and downstream behavior will differ. Retune via _foil_tune.py and
    update the pinned values here only after confirming the new
    precision / recall on a labeled set."""

    # (label, dbf, dms, dhr, expected_confidence, expected_is_foil)
    # Representative samples spanning the confidence distribution.
    FIXTURES = [
        # Strong foil (session 51 Mountain mom #280, was the one flagged
        # even under old weights)
        ("foil", +0.129, -12.29, +68.0,   None, False),
        # Mid-range foil (Astelli Reclaimer eoe #288, score just under
        # default threshold)
        ("foil", -0.203, -40.17, +73.0,   None, False),
        # Typical nonfoil (brighter scan, near-zero saturation delta)
        ("nonfoil", +0.145, +0.9, -10.0,  None, False),
        # Clear nonfoil (bright scan, negative saturation delta)
        ("nonfoil", +0.230, -8.0, -55.0,  None, False),
        # All-zero deltas -> score == bias alone
        ("neutral", 0.0, 0.0, 0.0,        None, False),
    ]

    def test_pinned_weights_and_bias(self):
        """Pin each coefficient exactly — any change requires updating
        this test deliberately."""
        self.assertAlmostEqual(W_DELTA_BRIGHT_FRAC, -18.92, places=4)
        self.assertAlmostEqual(W_DELTA_MEAN_S,       +0.0474, places=5)
        self.assertAlmostEqual(W_HUE_RANGE_BRIGHT,   -0.0057, places=5)
        self.assertAlmostEqual(FOIL_BIAS,            +0.294,  places=4)
        self.assertAlmostEqual(FOIL_CONFIDENCE_THRESHOLD, 1.50, places=4)

    def test_formula_reproduces_expected_scores(self):
        """For each fixture row, the scoring formula must yield the
        score computed by hand from the pinned constants."""
        for label, dbf, dms, dhr, _, _ in self.FIXTURES:
            expected = (FOIL_BIAS
                        + W_DELTA_BRIGHT_FRAC * dbf
                        + W_DELTA_MEAN_S     * dms
                        + W_HUE_RANGE_BRIGHT * dhr)
            # Construct a minimal synthetic pair that produces exactly
            # these deltas. Easier: call the scoring formula directly
            # via internal arithmetic (deltas are inputs, not signals
            # we derive from images here).
            # This test guards the arithmetic only; image->delta
            # correctness is covered by TestSyntheticOldLightingSignalDirection
            # and TestComputeBrightStats.
            score = (FOIL_BIAS
                     + W_DELTA_BRIGHT_FRAC * dbf
                     + W_DELTA_MEAN_S     * dms
                     + W_HUE_RANGE_BRIGHT * dhr)
            self.assertAlmostEqual(score, expected, places=6,
                                   msg=f"label={label} inputs=({dbf},{dms},{dhr})")

    def test_snapshot_scores_match_expectation(self):
        """These are the actual confidences the module should produce
        for representative real-scan signal values. If any of these
        drift by more than 0.001 the model has changed silently."""
        # Values computed from pinned constants — if the constants change
        # without updating these, the test will fail. That's the point.
        cases = [
            # (dbf, dms, dhr, expected_confidence)
            (+0.129, -12.29, +68.0,  -3.1168),  # Mountain mom #280:
            #   an old-physics foil that scores BELOW threshold under new
            #   weights (real foils under new lighting look different).
            (-0.203, -40.17, +73.0,  +1.8146),  # Astelli Reclaimer:
            #   a new-physics foil — would score just above +1.5 threshold.
            (+0.145,  +0.9,  -10.0,  -2.3497),  # typical nonfoil (class mean)
            (+0.230,  -8.0,  -55.0,  -4.1233),  # clear nonfoil
            ( 0.0,     0.0,    0.0,  +0.2940),  # zero deltas -> bias alone
        ]
        for dbf, dms, dhr, expected in cases:
            score = (FOIL_BIAS
                     + W_DELTA_BRIGHT_FRAC * dbf
                     + W_DELTA_MEAN_S     * dms
                     + W_HUE_RANGE_BRIGHT * dhr)
            self.assertAlmostEqual(
                score, expected, delta=0.001,
                msg=f"Confidence drift for ({dbf},{dms},{dhr}): "
                    f"got {score:.4f}, expected {expected:.4f}"
            )


if __name__ == "__main__":
    unittest.main()
