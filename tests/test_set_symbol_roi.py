"""Unit tests for set_symbol_roi.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import set_symbol_roi as ssr


class TestFrameLookup(unittest.TestCase):

    def test_1993_has_no_symbol(self):
        self.assertIsNone(ssr.get_symbol_roi("1993"))

    def test_1997_returns_art_box_roi(self):
        roi = ssr.get_symbol_roi("1997")
        self.assertIsNotNone(roi)
        x, y, w, h = roi
        # 1997 symbol sits in the art box region (y < 600), on the right
        self.assertLess(y, 600)
        self.assertGreater(x, 500)

    def test_2003_and_2015_type_line_roi(self):
        roi_2003 = ssr.get_symbol_roi("2003")
        roi_2015 = ssr.get_symbol_roi("2015")
        for roi in (roi_2003, roi_2015):
            self.assertIsNotNone(roi)
            x, y, w, h = roi
            # Modern set symbols sit on the right of the type line,
            # which lives below the art box (y > 520)
            self.assertGreater(y, 520)
            self.assertGreater(x, 500)

    def test_future_frame_falls_back_to_modern(self):
        self.assertEqual(
            ssr.get_symbol_roi("future"),
            ssr.get_symbol_roi("2015"),
        )

    def test_roi_fits_within_card_bounds(self):
        """Every non-None ROI must fit inside a 745x1040 image."""
        for frame in ssr.FRAME_ROI:
            roi = ssr.get_symbol_roi(frame)
            if roi is None:
                continue
            x, y, w, h = roi
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, 745)
            self.assertLessEqual(y + h, 1040)
            self.assertGreater(w, 0)
            self.assertGreater(h, 0)

    def test_unknown_frame_returns_none(self):
        self.assertIsNone(ssr.get_symbol_roi("gibberish"))

    def test_missing_frame_returns_none(self):
        self.assertIsNone(ssr.get_symbol_roi(None))


class TestFrameEffects(unittest.TestCase):

    def test_borderless_disables_symbol(self):
        self.assertIsNone(ssr.get_symbol_roi("2015", ["borderless"]))

    def test_showcase_disables_symbol(self):
        self.assertIsNone(ssr.get_symbol_roi("2015", ["showcase"]))

    def test_borderless_wins_over_2015(self):
        # Even on 2015 frame (which usually has a symbol), borderless
        # trumps it.
        self.assertIsNone(
            ssr.get_symbol_roi("2015", ["legendary", "borderless"])
        )

    def test_extendedart_keeps_base_roi(self):
        base = ssr.get_symbol_roi("2015")
        self.assertEqual(
            ssr.get_symbol_roi("2015", ["extendedart"]),
            base,
        )

    def test_cosmetic_effects_keep_base_roi(self):
        base = ssr.get_symbol_roi("2015")
        for effect in ["inverted", "colorshifted", "legendary", "nyxtouched"]:
            with self.subTest(effect=effect):
                self.assertEqual(
                    ssr.get_symbol_roi("2015", [effect]),
                    base,
                )

    def test_case_insensitive_effect_matching(self):
        self.assertIsNone(ssr.get_symbol_roi("2015", ["Borderless"]))
        self.assertIsNone(ssr.get_symbol_roi("2015", ["BORDERLESS"]))

    def test_empty_effects_returns_base(self):
        self.assertEqual(
            ssr.get_symbol_roi("2015", []),
            ssr.get_symbol_roi("2015"),
        )

    def test_none_effects_returns_base(self):
        self.assertEqual(
            ssr.get_symbol_roi("2015", None),
            ssr.get_symbol_roi("2015"),
        )


class TestHasSymbol(unittest.TestCase):

    def test_modern_card_has_symbol(self):
        card = {"frame": "2015", "frame_effects": []}
        self.assertTrue(ssr.has_symbol(card))

    def test_borderless_card_has_no_symbol(self):
        card = {"frame": "2015", "frame_effects": ["borderless"]}
        self.assertFalse(ssr.has_symbol(card))

    def test_alpha_card_has_no_symbol(self):
        card = {"frame": "1993"}
        self.assertFalse(ssr.has_symbol(card))

    def test_missing_frame_has_no_symbol(self):
        self.assertFalse(ssr.has_symbol({}))


if __name__ == "__main__":
    unittest.main()
