"""Unit tests for frame_detect.py.

Uses real card images from downloaded_cards/ as test inputs. Each
sample has a known (frame, frame_effects) from the Scryfall bulk data.
detect_frame() is expected to pick that combo when scored against a
set of candidate combos.

Note: the sample cards are present in frame_signatures.json (that's
where the reference data comes from), so exact-match distance can
approach 0 — that's expected. The discriminative test is whether, given
multiple candidate combos, detect_frame prefers the correct one by a
clear margin.
"""

import os
import sys
import unittest

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import frame_detect

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOWNLOADED_CARDS_DIR = os.path.join(_SCRIPT_DIR, "downloaded_cards")


# (card_id, expected_frame, expected_frame_effects_tuple)
# Chosen randomly from the Scryfall bulk data, filtered to ids we have
# locally in downloaded_cards/. See select script in commit message.
SAMPLES = [
    ("bf2efdd9-d2b4-4bea-a5b9-dbb2eee4dfba", "2015", ()),
    ("166ee7fc-a0f7-49af-85bf-d5b36bb2bff4", "2015", ()),
    ("5b340426-b852-43a5-8868-d5dacfdaf031", "2003", ()),
    ("4fe8c0b9-fdf4-4fc0-aa7c-774546cdd792", "2003", ()),
    ("1baf2a6c-57ec-4b38-8b08-4b3f800dbe99", "1997", ()),
    ("af7a2719-7910-4601-be88-7b3c249199d3", "1997", ()),
    ("ac2655e4-3a4d-4f73-820a-02fab675d42e", "1993", ()),
    ("c2ea6dfe-64d6-451a-bd34-31546996e711", "1993", ()),
    ("e18fd49d-c91e-458e-9bf3-13724ec404f4", "2015", ("extendedart",)),
    ("0a70bb7a-ba29-4fa5-b5f7-e2c71ca7df6f", "2015", ("showcase",)),
    ("5f917d8d-7037-4f11-91f6-1ef96b3541bb", "2015", ("fullart",)),
    ("ee6b54de-8e79-4f60-b642-3d259bf74ea0", "2015", ("legendary",)),
]

ALL_CANDIDATE_COMBOS = sorted({(frame, fe) for _, frame, fe in SAMPLES})


def _load_card_image(card_id):
    path = os.path.join(_DOWNLOADED_CARDS_DIR, f"{card_id}.png")
    if not os.path.exists(path):
        return None
    return cv2.imread(path, cv2.IMREAD_COLOR)


class TestDetectFrameFullCatalog(unittest.TestCase):
    """Detect frame when scored against the full signature catalog.

    Each sample's phash is present in frame_signatures.json, so its
    own combo should score very close to 0 and win decisively.
    """

    def test_each_sample_picks_correct_combo(self):
        missing = []
        for card_id, expected_frame, expected_fe in SAMPLES:
            img = _load_card_image(card_id)
            if img is None:
                missing.append(card_id)
                continue
            with self.subTest(card_id=card_id):
                result = frame_detect.detect_frame(img)
                self.assertEqual(
                    result["best_frame"], expected_frame,
                    f"{card_id}: expected frame {expected_frame}, "
                    f"got {result['best_frame']} (dist={result['distance']})"
                )
                self.assertEqual(
                    result["best_frame_effects"], expected_fe,
                    f"{card_id}: expected effects {expected_fe}, "
                    f"got {result['best_frame_effects']} (dist={result['distance']})"
                )

        if missing:
            self.skipTest(
                f"{len(missing)} sample image(s) missing from downloaded_cards/; "
                f"first: {missing[0]}"
            )


class TestDetectFrameRestrictedCandidates(unittest.TestCase):
    """Detect frame when scored only against a small candidate list —
    the realistic scenario where phash has returned 2-5 printings of
    the same art with different frames.
    """

    def test_each_sample_picks_correct_combo_from_candidates(self):
        missing = []
        for card_id, expected_frame, expected_fe in SAMPLES:
            img = _load_card_image(card_id)
            if img is None:
                missing.append(card_id)
                continue
            with self.subTest(card_id=card_id):
                result = frame_detect.detect_frame(
                    img, candidate_combos=ALL_CANDIDATE_COMBOS
                )
                self.assertEqual(
                    (result["best_frame"], result["best_frame_effects"]),
                    (expected_frame, expected_fe),
                    f"{card_id}: restricted-candidate pick wrong. "
                    f"dist={result['distance']} margin={result['margin']}"
                )
                # With the correct combo in the candidate list, margin
                # should be clearly positive — the correct combo is in
                # the reference set and will score near-zero distance.
                self.assertGreater(
                    result["margin"], 0,
                    f"{card_id}: expected positive margin, "
                    f"got {result['margin']}"
                )

        if missing:
            self.skipTest(
                f"{len(missing)} sample image(s) missing from downloaded_cards/"
            )


class TestFrameEffectsKey(unittest.TestCase):
    """Helpers convert frame_effects to the JSON key format correctly."""

    def test_empty_effects_list(self):
        self.assertEqual(frame_detect._frame_effects_key([]), "")

    def test_none_effects(self):
        self.assertEqual(frame_detect._frame_effects_key(None), "")

    def test_single_effect(self):
        self.assertEqual(frame_detect._frame_effects_key(["showcase"]), "showcase")

    def test_multiple_effects_sorted(self):
        self.assertEqual(
            frame_detect._frame_effects_key(["legendary", "showcase"]),
            "legendary,showcase",
        )

    def test_effects_key_is_deterministic(self):
        self.assertEqual(
            frame_detect._frame_effects_key(["showcase", "legendary"]),
            frame_detect._frame_effects_key(["legendary", "showcase"]),
        )


class TestFrameComboFromCard(unittest.TestCase):
    def test_basic_card(self):
        card = {"frame": "2015", "frame_effects": ["extendedart"]}
        self.assertEqual(
            frame_detect.frame_combo_from_card(card),
            ("2015", ("extendedart",)),
        )

    def test_missing_effects(self):
        card = {"frame": "1997"}
        self.assertEqual(
            frame_detect.frame_combo_from_card(card),
            ("1997", ()),
        )

    def test_null_effects(self):
        card = {"frame": "2003", "frame_effects": None}
        self.assertEqual(
            frame_detect.frame_combo_from_card(card),
            ("2003", ()),
        )


if __name__ == "__main__":
    unittest.main()
