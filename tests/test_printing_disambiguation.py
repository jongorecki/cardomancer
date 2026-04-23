"""Unit tests for printing_disambiguation.py.

Two test layers:

1. Pure-cascade tests (`TestRunCascade`): call `_run_cascade` directly
   with mocked candidate-card dicts, exercising the source-label and
   filtering logic without loading the full Scryfall bulk data or
   running the real detectors. Detectors are monkey-patched.

2. End-to-end tests (`TestDisambiguatePrinting`): call the public
   `disambiguate_printing()` with real downloaded card images. Real
   detectors run; real Scryfall data resolves candidates. Skipped if
   the reference images aren't present locally.
"""

import os
import sys
import unittest
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import printing_disambiguation as pd

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOWNLOADED_CARDS_DIR = os.path.join(_SCRIPT_DIR, "downloaded_cards")


def _card(id_, set_, frame="2015", frame_effects=None, usd=None):
    """Build a minimal Scryfall-shaped card dict for cascade tests."""
    return {
        "id": id_,
        "set": set_,
        "frame": frame,
        "frame_effects": frame_effects or [],
        "prices": {"usd": usd} if usd else {},
    }


def _dummy_img():
    """Placeholder image — cascade stages are mocked so contents don't matter."""
    return np.zeros((1040, 745, 3), dtype=np.uint8)


class TestRunCascade(unittest.TestCase):
    """Exercise _run_cascade() with mocked detectors and fake cards."""

    def test_empty_candidates_returns_input_id(self):
        result = pd._run_cascade(_dummy_img(), "id-1", [])
        self.assertEqual(result["final_card_id"], "id-1")
        self.assertEqual(result["source"], pd.SOURCE_SINGLE_MATCH)
        self.assertEqual(result["candidates_surviving"], [])

    def test_single_candidate_returns_single_match(self):
        cards = [_card("id-1", "xln")]
        result = pd._run_cascade(_dummy_img(), "id-1", cards)
        self.assertEqual(result["final_card_id"], "id-1")
        self.assertEqual(result["source"], pd.SOURCE_SINGLE_MATCH)
        # Stages were not run
        self.assertIsNone(result["frame_confidence"])
        self.assertIsNone(result["stamp_confidence"])

    def test_two_candidates_same_frame_skips_stage1(self):
        """Both cards share frame -> detect_frame should NOT be called."""
        cards = [_card("id-1", "xln", frame="2015"),
                 _card("id-2", "plst", frame="2015")]
        with patch.object(pd, "detect_frame") as mock_frame, \
             patch.object(pd, "detect_list_stamp",
                          return_value=(True, 0.9)) as mock_stamp:
            pd._run_cascade(_dummy_img(), "id-1", cards)
            mock_frame.assert_not_called()
            mock_stamp.assert_called_once()

    def test_two_candidates_different_frame_fires_stage1(self):
        cards = [_card("id-1", "xln", frame="2015"),
                 _card("id-2", "dmr", frame="1997")]
        with patch.object(
            pd, "detect_frame",
            return_value={
                "best_frame": "2015",
                "best_frame_effects": (),
                "distance": 2,
                "margin": 30.0,
                "confidence": 1.0,
                "scores": None,
            },
        ) as mock_frame:
            result = pd._run_cascade(_dummy_img(), "id-1", cards)
            mock_frame.assert_called_once()
            self.assertEqual(result["source"], pd.SOURCE_FRAME_DISAMBIGUATED)
            self.assertEqual(result["final_card_id"], "id-1")
            self.assertEqual(result["candidates_surviving"], ["id-1"])

    def test_low_frame_confidence_does_not_commit(self):
        """Frame confidence below threshold -> no filtering, fall through."""
        cards = [_card("id-1", "xln", frame="2015", usd="0.50"),
                 _card("id-2", "dmr", frame="1997", usd="1.00")]
        with patch.object(
            pd, "detect_frame",
            return_value={
                "best_frame": "2015",
                "best_frame_effects": (),
                "distance": 10,
                "margin": 5.0,
                "confidence": 0.1,
                "scores": None,
            },
        ):
            result = pd._run_cascade(_dummy_img(), "id-1", cards)
            # Neither detector committed -> cheapest fallback
            self.assertEqual(result["source"], pd.SOURCE_CHEAPEST_FALLBACK)
            self.assertEqual(result["final_card_id"], "id-1")  # cheapest

    def test_stamp_high_confidence_picks_list(self):
        cards = [_card("id-1", "xln", frame="2015"),
                 _card("id-2", "plst", frame="2015")]
        with patch.object(pd, "detect_list_stamp", return_value=(True, 0.9)):
            result = pd._run_cascade(_dummy_img(), "id-1", cards)
            self.assertEqual(result["source"], pd.SOURCE_STAMP_DISAMBIGUATED)
            self.assertEqual(result["final_card_id"], "id-2")
            self.assertEqual(result["stamp_has_stamp"], True)

    def test_stamp_clear_negative_picks_non_list(self):
        """Very low stamp confidence -> commit to non-list."""
        cards = [_card("id-1", "xln", frame="2015"),
                 _card("id-2", "plst", frame="2015")]
        with patch.object(pd, "detect_list_stamp", return_value=(False, 0.02)):
            result = pd._run_cascade(_dummy_img(), "id-1", cards)
            self.assertEqual(result["source"], pd.SOURCE_STAMP_DISAMBIGUATED)
            self.assertEqual(result["final_card_id"], "id-1")
            self.assertEqual(result["stamp_has_stamp"], False)

    def test_stamp_mid_confidence_no_commit(self):
        """Mid-range stamp confidence -> don't filter."""
        cards = [_card("id-1", "xln", frame="2015", usd="0.25"),
                 _card("id-2", "plst", frame="2015", usd="0.50")]
        with patch.object(pd, "detect_list_stamp", return_value=(False, 0.20)):
            result = pd._run_cascade(_dummy_img(), "id-1", cards)
            self.assertEqual(result["source"], pd.SOURCE_CHEAPEST_FALLBACK)
            # Both survived -> cheapest (id-1 @ 0.25) wins
            self.assertEqual(result["final_card_id"], "id-1")

    def test_frame_and_stamp_both_commit_fully_disambiguated(self):
        cards = [_card("id-1", "xln", frame="1997"),
                 _card("id-2", "plst", frame="2015"),
                 _card("id-3", "dmr", frame="2015")]
        with patch.object(
            pd, "detect_frame",
            return_value={
                "best_frame": "2015",
                "best_frame_effects": (),
                "distance": 1,
                "margin": 25.0,
                "confidence": 0.9,
                "scores": None,
            },
        ), patch.object(pd, "detect_list_stamp", return_value=(True, 0.9)):
            result = pd._run_cascade(_dummy_img(), "id-1", cards)
            self.assertEqual(result["source"], pd.SOURCE_FULLY_DISAMBIGUATED)
            self.assertEqual(result["final_card_id"], "id-2")

    def test_icon_commits_when_sets_disagree(self):
        """Two same-frame, non-list candidates disagreeing on set -> Stage 3 fires."""
        cards = [_card("id-1", "dmu", frame="2015"),
                 _card("id-2", "neo", frame="2015")]
        with patch.object(pd, "identify_set_icon",
                          return_value=("dmu", 0.9)) as mock_icon:
            result = pd._run_cascade(_dummy_img(), "id-2", cards)
            mock_icon.assert_called_once()
            self.assertEqual(result["source"], pd.SOURCE_ICON_DISAMBIGUATED)
            self.assertEqual(result["final_card_id"], "id-1")
            self.assertEqual(result["icon_set_pick"], "dmu")
            self.assertEqual(result["icon_confidence"], 0.9)

    def test_icon_low_confidence_does_not_commit(self):
        """identify_set_icon returns (None, conf) -> no filtering; cheapest fallback."""
        cards = [_card("id-1", "dmu", frame="2015", usd="0.25"),
                 _card("id-2", "neo", frame="2015", usd="0.50")]
        with patch.object(pd, "identify_set_icon",
                          return_value=(None, 0.3)):
            result = pd._run_cascade(_dummy_img(), "id-1", cards)
            self.assertEqual(result["source"], pd.SOURCE_CHEAPEST_FALLBACK)
            self.assertEqual(result["icon_confidence"], 0.3)
            self.assertIsNone(result["icon_set_pick"])

    def test_icon_skipped_when_all_survivors_share_set(self):
        """Same-set candidates (shouldn't happen, but if it does) -> Stage 3 skipped."""
        cards = [_card("id-1", "dmu", frame="2015"),
                 _card("id-2", "dmu", frame="2015")]
        with patch.object(pd, "identify_set_icon") as mock_icon:
            pd._run_cascade(_dummy_img(), "id-1", cards)
            mock_icon.assert_not_called()

    def test_frame_plus_icon_fully_disambiguated(self):
        """Stage 1 + Stage 3 both commit -> fully_disambiguated."""
        cards = [_card("id-1", "dmu", frame="2015"),
                 _card("id-2", "neo", frame="2015"),
                 _card("id-3", "lea", frame="1993")]
        with patch.object(
            pd, "detect_frame",
            return_value={
                "best_frame": "2015",
                "best_frame_effects": (),
                "distance": 1,
                "margin": 25.0,
                "confidence": 0.9,
                "scores": None,
            },
        ), patch.object(pd, "identify_set_icon",
                        return_value=("neo", 0.88)):
            result = pd._run_cascade(_dummy_img(), "id-1", cards)
            self.assertEqual(result["source"], pd.SOURCE_FULLY_DISAMBIGUATED)
            self.assertEqual(result["final_card_id"], "id-2")

    def test_all_filtered_out_falls_back(self):
        """Detectors erroneously filter everything -> fallback to cheapest."""
        cards = [_card("id-1", "xln", frame="2015", usd="0.10"),
                 _card("id-2", "dmr", frame="2015", usd="0.50")]
        with patch.object(
            pd, "detect_frame",
            return_value={
                "best_frame": "1997",   # neither candidate has this frame
                "best_frame_effects": (),
                "distance": 1,
                "margin": 25.0,
                "confidence": 0.9,
                "scores": None,
            },
        ):
            result = pd._run_cascade(_dummy_img(), "id-1", cards)
            self.assertEqual(result["source"], pd.SOURCE_CHEAPEST_FALLBACK)
            # Fallback uses cheapest of original candidates
            self.assertEqual(result["final_card_id"], "id-1")

    def test_cheapest_among_survivors(self):
        cards = [_card("id-1", "xln", usd="5.00"),
                 _card("id-2", "dmr", usd="0.50"),
                 _card("id-3", "cmm", usd="1.00")]
        self.assertEqual(pd._cheapest_among(cards), "id-2")

    def test_cheapest_among_all_priceless_returns_none(self):
        cards = [_card("id-1", "xln"), _card("id-2", "dmr")]
        self.assertIsNone(pd._cheapest_among(cards))


class TestDisambiguatePrinting(unittest.TestCase):
    """End-to-end tests with real images and real Scryfall data."""

    def _load(self, card_id):
        path = os.path.join(_DOWNLOADED_CARDS_DIR, f"{card_id}.png")
        if not os.path.exists(path):
            return None
        return cv2.imread(path, cv2.IMREAD_COLOR)

    def test_plst_scan_resolves_to_plst(self):
        """Scan of a plst card with non-list siblings -> cascade picks plst."""
        plst_id = "003feb09-600a-46ff-90a2-7606636d6c53"  # Courage in Crisis plst
        img = self._load(plst_id)
        if img is None:
            self.skipTest(f"Missing {plst_id}.png")
        result = pd.disambiguate_printing(img, plst_id)
        self.assertEqual(result["final_card_id"], plst_id)
        self.assertIn(
            result["source"],
            (pd.SOURCE_STAMP_DISAMBIGUATED, pd.SOURCE_FULLY_DISAMBIGUATED),
            f"Unexpected source: {result['source']}",
        )
        self.assertTrue(result["stamp_has_stamp"])

    def test_non_plst_scan_does_not_get_remapped_to_plst(self):
        """Scan of a non-list card whose art has a plst sibling -> keeps
        original, doesn't misremap to plst."""
        dmu_id = "000376ef-8b6c-490d-98cb-d6de15b2e585"  # Battlewing Mystic dmu
        img = self._load(dmu_id)
        if img is None:
            self.skipTest(f"Missing {dmu_id}.png")
        result = pd.disambiguate_printing(img, dmu_id)
        surviving = result["candidates_surviving"]
        # Should NOT include the plst sibling after disambiguation
        self.assertIn(dmu_id, surviving)
        # Check a plst sibling exists in original candidates — proves the
        # cascade had something to filter against
        plst_sibling = "09d4f09c-de87-49a5-9c8b-2bc73595eda5"
        self.assertIn(
            plst_sibling, result["candidates_considered"],
            "Test setup broken: plst sibling not in candidate list"
        )
        self.assertNotIn(plst_sibling, surviving)
        self.assertEqual(result["stamp_has_stamp"], False)

    def test_missing_card_id_returns_no_candidates(self):
        result = pd.disambiguate_printing(_dummy_img(), None)
        self.assertEqual(result["source"], pd.SOURCE_NO_CANDIDATES)
        self.assertIsNone(result["final_card_id"])

    def test_unknown_card_id_returns_no_art_group(self):
        result = pd.disambiguate_printing(
            _dummy_img(), "ffffffff-ffff-ffff-ffff-ffffffffffff"
        )
        self.assertEqual(result["source"], pd.SOURCE_NO_ART_GROUP)


if __name__ == "__main__":
    unittest.main()
