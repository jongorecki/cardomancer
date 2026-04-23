"""Unit tests for set_icon.py.

Synthetic tests construct a card image that embeds a known set's
averaged-from-PNGs grayscale template inside the modern-frame ROI.
Integration tests that need real card scans live in
tests/test_printing_disambiguation.py.
"""

import os
import sys
import unittest

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import set_icon as si
from set_symbol_roi import get_symbol_roi


def _blank_card() -> np.ndarray:
    return np.zeros((1040, 745, 3), dtype=np.uint8)


def _paint_template_into_roi(
    card: np.ndarray,
    template_gray: np.ndarray,
    roi: tuple,
) -> np.ndarray:
    """Paste a grayscale template into the (x, y, w, h) ROI of a BGR card."""
    x, y, w, h = roi
    th, tw = template_gray.shape[:2]
    ox = x + max(0, (w - tw) // 2)
    oy = y + max(0, (h - th) // 2)
    card[oy:oy + th, ox:ox + tw] = np.stack([template_gray] * 3, axis=-1)
    return card


# Use dmu — dmu_2015_edge.png and dmu_2015_gray.png are both built by
# build_png_roi_templates.py and the set has only one frame treatment.
_DMU_EDGE = os.path.join(si._TEMPLATE_DIR, "dmu_2015_edge.png")
_DMU_GRAY = os.path.join(si._TEMPLATE_DIR, "dmu_2015_gray.png")


class TestLoadTemplatePair(unittest.TestCase):

    def test_unknown_set_returns_none_pair(self):
        e, g = si._load_template_pair("zzz_not_a_set", "2015")
        self.assertIsNone(e)
        self.assertIsNone(g)

    def test_known_set_returns_arrays(self):
        if not (os.path.exists(_DMU_EDGE) and os.path.exists(_DMU_GRAY)):
            self.skipTest("roi_templates cache missing — "
                          "run build_png_roi_templates.py first")
        e, g = si._load_template_pair("dmu", "2015")
        self.assertIsNotNone(e)
        self.assertIsNotNone(g)
        # Both templates were built from the same ROI, so same shape.
        self.assertEqual(e.shape, g.shape)


class TestIdentifySetIcon(unittest.TestCase):

    def setUp(self):
        if not (os.path.exists(_DMU_EDGE) and os.path.exists(_DMU_GRAY)):
            self.skipTest("roi_templates cache missing — "
                          "run build_png_roi_templates.py first")
        self.dmu_gray = cv2.imread(_DMU_GRAY, cv2.IMREAD_GRAYSCALE)

    def test_borderless_short_circuits(self):
        card = _blank_card()
        pick, conf = si.identify_set_icon(
            card, ["dmu", "mom"], frame="2015",
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
            card, ["dmu", "mom"], frame="2015",
        )
        self.assertIsNone(pick)
        self.assertEqual(conf, 0.0)

    def test_template_planted_in_roi_is_recovered(self):
        """Paint dmu's gray template into the 2015-frame ROI; dmu wins."""
        card = _blank_card()
        roi = get_symbol_roi("2015")
        card = _paint_template_into_roi(card, self.dmu_gray, roi)

        pick, conf = si.identify_set_icon(
            card, ["dmu", "mom", "one"], frame="2015",
        )
        self.assertEqual(pick, "dmu")
        self.assertGreaterEqual(conf, si.SET_ICON_THRESHOLD)

    def test_missing_candidate_templates_dont_crash(self):
        """A candidate with no cached template should be silently skipped."""
        card = _blank_card()
        roi = get_symbol_roi("2015")
        card = _paint_template_into_roi(card, self.dmu_gray, roi)
        pick, conf = si.identify_set_icon(
            card, ["dmu", "zzz_not_a_set"], frame="2015",
        )
        # dmu is still the only real candidate; with only one scorer the
        # margin test is skipped and dmu wins on threshold alone.
        self.assertEqual(pick, "dmu")


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
