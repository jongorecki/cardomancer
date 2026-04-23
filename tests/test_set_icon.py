"""Unit tests for set_icon.py.

Synthetic tests construct a card image that embeds a known set's edge
template inside the modern-frame ROI. Integration tests that need real
card scans live in tests/test_printing_disambiguation.py.
"""

import os
import sys
import unittest
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import set_icon as si


def _blank_card() -> np.ndarray:
    return np.zeros((1040, 745, 3), dtype=np.uint8)


def _paint_template_into_roi(
    card: np.ndarray,
    template_gray: np.ndarray,
    roi: tuple,
) -> np.ndarray:
    """Paste a grayscale template into the (x, y, w, h) ROI of a BGR card."""
    x, y, w, h = roi
    # Center the template inside the ROI (the ROI is larger than the
    # template by design).
    th, tw = template_gray.shape[:2]
    ox = x + (w - tw) // 2
    oy = y + (h - th) // 2
    card[oy:oy + th, ox:ox + tw] = np.stack([template_gray] * 3, axis=-1)
    return card


class TestLoadTemplate(unittest.TestCase):

    def test_unknown_set_returns_none(self):
        self.assertIsNone(si._load_template("zzz_not_a_set"))

    def test_known_set_returns_array(self):
        # xln is cached by fetch_set_symbols.py
        path = os.path.join(si._EDGE_DIR, "xln_32.png")
        if not os.path.exists(path):
            self.skipTest("set_symbols cache missing — "
                          "run fetch_set_symbols.py first")
        t = si._load_template("xln")
        self.assertIsNotNone(t)
        self.assertEqual(t.shape[0], si.TEMPLATE_SIZE)


class TestIdentifySetIcon(unittest.TestCase):

    def setUp(self):
        xln_path = os.path.join(si._EDGE_DIR, "xln_32.png")
        dmu_path = os.path.join(si._EDGE_DIR, "dmu_32.png")
        if not (os.path.exists(xln_path) and os.path.exists(dmu_path)):
            self.skipTest("set_symbols cache missing — "
                          "run fetch_set_symbols.py first")
        self.xln_template = cv2.imread(xln_path, cv2.IMREAD_GRAYSCALE)

    def test_borderless_short_circuits(self):
        card = _blank_card()
        pick, conf = si.identify_set_icon(
            card, ["xln", "dmu"], frame="2015",
            frame_effects=["borderless"],
        )
        self.assertIsNone(pick)
        self.assertEqual(conf, 0.0)

    def test_1993_frame_short_circuits(self):
        card = _blank_card()
        pick, conf = si.identify_set_icon(
            card, ["lea", "leb"], frame="1993",
        )
        self.assertIsNone(pick)
        self.assertEqual(conf, 0.0)

    def test_empty_candidates_returns_none(self):
        card = _blank_card()
        pick, conf = si.identify_set_icon(
            card, [], frame="2015",
        )
        self.assertIsNone(pick)
        self.assertEqual(conf, 0.0)

    def test_empty_roi_returns_none(self):
        """Pure black ROI -> Canny returns no edges -> short-circuit."""
        card = _blank_card()
        pick, conf = si.identify_set_icon(
            card, ["xln", "dmu"], frame="2015",
        )
        self.assertIsNone(pick)
        self.assertEqual(conf, 0.0)

    def test_template_planted_in_roi_is_recovered(self):
        """Paint xln's edge template into the 2015-frame ROI; xln should win."""
        card = _blank_card()
        # Need the ROI filled with edge-like signal. Paste the *grayscale
        # png* (not edge) so that running Canny on it reconstructs the
        # edges we'll compare against the cached edge template.
        png_path = os.path.join(
            os.path.dirname(si._EDGE_DIR), "png", "xln_32.png"
        )
        if not os.path.exists(png_path):
            self.skipTest("xln_32.png missing")
        xln_png = cv2.imread(png_path, cv2.IMREAD_GRAYSCALE)

        # Modern frame ROI: (660, 600, 70, 55)
        card = _paint_template_into_roi(card, xln_png, (660, 600, 70, 55))

        pick, conf = si.identify_set_icon(
            card, ["xln", "dmu", "neo"], frame="2015",
        )
        self.assertEqual(pick, "xln")
        self.assertGreaterEqual(conf, si.SET_ICON_THRESHOLD)

    def test_missing_candidate_templates_dont_crash(self):
        """A candidate with no cached template should be silently skipped."""
        card = _blank_card()
        png_path = os.path.join(
            os.path.dirname(si._EDGE_DIR), "png", "xln_32.png"
        )
        xln_png = cv2.imread(png_path, cv2.IMREAD_GRAYSCALE)
        card = _paint_template_into_roi(card, xln_png, (660, 600, 70, 55))
        pick, conf = si.identify_set_icon(
            card, ["xln", "zzz_not_a_set"], frame="2015",
        )
        # xln is still the only real candidate; with only one scorer the
        # margin test is skipped and xln wins on threshold alone.
        self.assertEqual(pick, "xln")


class TestTransformedTemplates(unittest.TestCase):

    def test_yields_expected_count(self):
        t = np.ones((10, 10), dtype=np.uint8)
        variants = list(si._transformed_templates(t))
        expected = len(si.SET_ICON_SCALES) * len(si.SET_ICON_ROTATIONS)
        self.assertEqual(len(variants), expected)

    def test_variants_preserve_dtype(self):
        t = np.ones((10, 10), dtype=np.uint8) * 255
        for v in si._transformed_templates(t):
            self.assertEqual(v.dtype, np.uint8)


if __name__ == "__main__":
    unittest.main()
