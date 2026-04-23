"""Unit tests for list_stamp.py.

Uses real card images from downloaded_cards/ as test inputs. Each
sample is either a list-style reprint (plst/ulst — has the
planeswalker stamp) or a non-list original (no stamp).
detect_list_stamp() is expected to correctly classify each.

Note: the plst template was built from Scryfall source images, so the
samples are the same visual domain as the template. A small tail of
DDR-layout plst cards (shifted stamp position) scores near threshold
but still classifies correctly.
"""

import os
import sys
import unittest

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import list_stamp

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOWNLOADED_CARDS_DIR = os.path.join(_SCRIPT_DIR, "downloaded_cards")


# (card_id, expected_has_stamp)
LIST_SAMPLES = [
    ("0004311b-646a-4df8-a4b4-9171642e9ef4", True),  # plst Admiral Beckett Brass
    ("00108a1c-e620-4124-9b31-3bb3ff2a0407", True),  # plst Altar's Reap (DDR layout)
    ("003feb09-600a-46ff-90a2-7606636d6c53", True),  # plst Courage in Crisis (template source)
    ("00560d67-5299-4681-952b-d153d82bedaf", True),  # plst Copy token
    ("006ebdeb-ae82-4de9-8f9e-f76f275ea811", True),  # plst Rite of Replication
    ("0070bbf6-fdee-44ec-bfb8-3e99d6338e6e", True),  # plst Sparkspitter
    ("01115986-1963-45c6-b706-2faee6c71e8f", True),  # plst Propaganda
]

NON_LIST_SAMPLES = [
    ("0000419b-0bba-4488-8f7a-6194544ce91e", False),  # blb Forest
    ("0000a54c-a511-4925-92dc-01b937f9afad", False),  # tmm2 Spirit
    ("0000cd57-91fe-411f-b798-646e965eec37", False),  # xln Siren Lookout
    ("0001b4a6-10bf-4bdd-a9da-fe78d9d39a82", False),  # pdft Wastewood Verge
    ("0001c639-8bd0-426f-89cb-4ca61f3cc054", False),  # who Surge of Brilliance
    ("0001e77a-7fff-49d2-a55c-42f6fdf6db08", False),  # woe Obyra's Attendants
    ("0001f1ef-b957-4a55-b47f-14839cdbab6f", False),  # eld Venerable Knight
    ("00020b05-ecb9-4603-8cc1-8cfa7a14befc", False),  # ugin Wildcall
]


def _load_card_image(card_id):
    path = os.path.join(_DOWNLOADED_CARDS_DIR, f"{card_id}.png")
    if not os.path.exists(path):
        return None
    return cv2.imread(path, cv2.IMREAD_COLOR)


class TestDetectListStamp(unittest.TestCase):
    """detect_list_stamp() classifies plst vs non-plst sample images."""

    def test_list_samples_detect_stamp(self):
        missing = []
        for card_id, expected in LIST_SAMPLES:
            img = _load_card_image(card_id)
            if img is None:
                missing.append(card_id)
                continue
            with self.subTest(card_id=card_id):
                has_stamp, conf = list_stamp.detect_list_stamp(img)
                self.assertEqual(
                    has_stamp, expected,
                    f"{card_id}: expected has_stamp={expected}, "
                    f"got {has_stamp} (conf={conf:.3f})"
                )
        if missing:
            self.skipTest(
                f"{len(missing)} sample image(s) missing; first: {missing[0]}"
            )

    def test_non_list_samples_reject_stamp(self):
        missing = []
        for card_id, expected in NON_LIST_SAMPLES:
            img = _load_card_image(card_id)
            if img is None:
                missing.append(card_id)
                continue
            with self.subTest(card_id=card_id):
                has_stamp, conf = list_stamp.detect_list_stamp(img)
                self.assertEqual(
                    has_stamp, expected,
                    f"{card_id}: expected has_stamp={expected}, "
                    f"got {has_stamp} (conf={conf:.3f})"
                )
        if missing:
            self.skipTest(
                f"{len(missing)} sample image(s) missing; first: {missing[0]}"
            )

    def test_confidence_separates_classes(self):
        """Mean confidence on list samples should clearly exceed
        mean confidence on non-list samples."""
        list_confs = []
        non_confs = []
        for card_id, _ in LIST_SAMPLES:
            img = _load_card_image(card_id)
            if img is not None:
                _, conf = list_stamp.detect_list_stamp(img)
                list_confs.append(conf)
        for card_id, _ in NON_LIST_SAMPLES:
            img = _load_card_image(card_id)
            if img is not None:
                _, conf = list_stamp.detect_list_stamp(img)
                non_confs.append(conf)
        if not list_confs or not non_confs:
            self.skipTest("Not enough sample images available")
        min_list = min(list_confs)
        max_non = max(non_confs)
        self.assertGreater(
            min_list, max_non,
            f"Expected min(list)={min_list:.3f} > max(non-list)={max_non:.3f}"
        )

    def test_resizes_non_standard_dimensions(self):
        """detect_list_stamp() should resize and still classify."""
        img = _load_card_image(LIST_SAMPLES[0][0])
        if img is None:
            self.skipTest("Sample image missing")
        resized = cv2.resize(img, (400, 550))
        has_stamp, conf = list_stamp.detect_list_stamp(resized)
        self.assertTrue(
            has_stamp,
            f"Resized plst image should still detect stamp (conf={conf:.3f})"
        )


class TestIsListCandidatePair(unittest.TestCase):
    def test_plst_and_non_plst_returns_true(self):
        candidates = [{"set": "plst"}, {"set": "xln"}]
        self.assertTrue(list_stamp.is_list_candidate_pair(candidates))

    def test_ulst_and_non_list_returns_true(self):
        candidates = [{"set": "unf"}, {"set": "ulst"}]
        self.assertTrue(list_stamp.is_list_candidate_pair(candidates))

    def test_all_plst_returns_false(self):
        candidates = [{"set": "plst"}, {"set": "plst"}]
        self.assertFalse(list_stamp.is_list_candidate_pair(candidates))

    def test_all_non_list_returns_false(self):
        candidates = [{"set": "xln"}, {"set": "dmr"}, {"set": "m21"}]
        self.assertFalse(list_stamp.is_list_candidate_pair(candidates))

    def test_empty_returns_false(self):
        self.assertFalse(list_stamp.is_list_candidate_pair([]))

    def test_single_plst_returns_false(self):
        self.assertFalse(list_stamp.is_list_candidate_pair([{"set": "plst"}]))


if __name__ == "__main__":
    unittest.main()
