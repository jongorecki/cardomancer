"""Unit tests for foil_detect.py — multi-signal foil detection.

We synthesize card + reference pairs to verify that the detector:
  - Responds positively to the three known foil signatures (more bright
    pixels, desaturated hotspots, hue scatter in bright pixels)
  - Responds negatively to non-foil differences (art has bright saturated
    colors matching reference, no shimmer)
  - Degrades gracefully when inputs are missing or malformed
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


def _make_foil_scan_from_reference(ref_bgr):
    """Simulate a foil scan: start from reference, add specular hotspots
    that are brighter, desaturated, and hue-scattered."""
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

    def test_simulated_foil_scores_above_threshold(self):
        ref = _make_reference_normal()
        scan = _make_foil_scan_from_reference(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "ok")
        # Should have high confidence and flag as foil
        self.assertGreater(result["confidence"], FOIL_CONFIDENCE_THRESHOLD)
        self.assertTrue(result["is_foil"])

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
        scan = _make_foil_scan_from_reference(ref)
        result = detect_foil(scan, reference_img=ref_small)
        self.assertEqual(result["reason"], "ok")
        # Foil-like signal should survive the resize
        self.assertTrue(result["is_foil"])

    def test_result_contains_expected_signals(self):
        ref = _make_reference_normal()
        scan = _make_foil_scan_from_reference(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertIn("signals", result)
        s = result["signals"]
        self.assertIn("scan", s)
        self.assertIn("reference", s)
        self.assertIn("delta_bright_frac", s)
        self.assertIn("delta_mean_s", s)
        self.assertIn("delta_hue_range", s)

    def test_custom_threshold_overrides_default(self):
        """Passing a higher threshold should suppress the foil flag even
        when the default would fire."""
        ref = _make_reference_normal()
        scan = _make_foil_scan_from_reference(ref)
        default = detect_foil(scan, reference_img=ref)
        high = detect_foil(scan, reference_img=ref, threshold=999.0)
        # Confidence is the same, but is_foil differs
        self.assertAlmostEqual(default["confidence"],
                               high["confidence"], places=5)
        self.assertTrue(default["is_foil"])
        self.assertFalse(high["is_foil"])


# ---------------------------------------------------------------------------
# Foil signal direction sanity checks
# ---------------------------------------------------------------------------

class TestFoilSignalDirection(unittest.TestCase):
    """Verify each delta moves in the expected direction for foils."""

    def setUp(self):
        self.ref = _make_reference_normal()
        self.foil = _make_foil_scan_from_reference(self.ref)
        self.result = detect_foil(self.foil, reference_img=self.ref)

    def test_delta_bright_frac_positive_for_foil(self):
        self.assertGreater(
            self.result["signals"]["delta_bright_frac"], 0,
            "Foil scan should have MORE bright pixels than reference"
        )

    def test_delta_mean_s_negative_for_foil(self):
        self.assertLess(
            self.result["signals"]["delta_mean_s"], 0,
            "Foil hotspots should desaturate (lower mean_s in bright pixels)"
        )

    def test_delta_hue_range_positive_for_foil(self):
        self.assertGreater(
            self.result["signals"]["delta_hue_range"], 0,
            "Foil should scatter hue (wider range in bright pixels)"
        )


if __name__ == "__main__":
    unittest.main()
