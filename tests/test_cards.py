"""Unit tests for cards.py — card info extraction and printings map."""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cards import (
    extract_card_info,
    card_is_allowed,
    get_illustration_id,
    CARDS_DATA,
    CARD_DATA_BY_ID,
    PRINTINGS_MAP,
)


class TestExtractCardInfo(unittest.TestCase):
    """Tests for extract_card_info. These depend on having card data loaded."""

    def test_nonexistent_card_returns_none(self):
        result = extract_card_info("nonexistent-id-12345")
        self.assertIsNone(result)

    @unittest.skipUnless(CARD_DATA_BY_ID, "No card data loaded — skipping")
    def test_real_card_has_required_fields(self):
        """Pick the first card in the DB and verify the returned dict structure."""
        card_id = next(iter(CARD_DATA_BY_ID))
        info = extract_card_info(card_id)
        self.assertIsNotNone(info)
        self.assertIn("Name", info)
        self.assertIn("Set", info)
        self.assertIn("Sets", info)
        self.assertIn("Colors", info)
        self.assertIn("Color Identity", info)
        self.assertIn("CMC", info)
        self.assertIn("Types", info)
        self.assertIn("Price", info)

    @unittest.skipUnless(CARD_DATA_BY_ID, "No card data loaded — skipping")
    def test_sets_is_list(self):
        card_id = next(iter(CARD_DATA_BY_ID))
        info = extract_card_info(card_id)
        self.assertIsInstance(info["Sets"], list)
        self.assertGreaterEqual(len(info["Sets"]), 1)

    @unittest.skipUnless(CARD_DATA_BY_ID, "No card data loaded — skipping")
    def test_types_are_valid(self):
        card_id = next(iter(CARD_DATA_BY_ID))
        info = extract_card_info(card_id)
        valid_types = {"creature", "artifact", "enchantment", "instant",
                       "sorcery", "battle", "planeswalker", "land"}
        for t in info["Types"]:
            self.assertIn(t, valid_types)


class TestCardIsAllowed(unittest.TestCase):

    def test_nonexistent_card(self):
        self.assertFalse(card_is_allowed("nonexistent-id-12345"))

    @unittest.skipUnless(CARD_DATA_BY_ID, "No card data loaded — skipping")
    def test_some_cards_are_allowed(self):
        """At least some cards in the DB should be allowed."""
        allowed_count = sum(1 for cid in list(CARD_DATA_BY_ID)[:1000]
                           if card_is_allowed(cid))
        self.assertGreater(allowed_count, 0,
                           "Expected at least some allowed cards in first 1000")


class TestGetIllustrationId(unittest.TestCase):

    def test_nonexistent_card(self):
        result = get_illustration_id("nonexistent-id-12345")
        self.assertIsNone(result)

    @unittest.skipUnless(CARD_DATA_BY_ID, "No card data loaded — skipping")
    def test_real_card_has_illustration_id(self):
        # Most cards have an illustration_id
        found = False
        for card_id in list(CARD_DATA_BY_ID)[:100]:
            if get_illustration_id(card_id) is not None:
                found = True
                break
        self.assertTrue(found, "Expected at least one card with illustration_id")


class TestPrintingsMap(unittest.TestCase):

    @unittest.skipUnless(PRINTINGS_MAP, "No printings map loaded — skipping")
    def test_printings_map_structure(self):
        """Verify the structure of printings map entries."""
        card_id = next(iter(PRINTINGS_MAP))
        entry = PRINTINGS_MAP[card_id]
        self.assertIn("name", entry)
        self.assertIn("illustration_id", entry)
        self.assertIn("printings", entry)
        self.assertIsInstance(entry["printings"], list)
        if entry["printings"]:
            printing = entry["printings"][0]
            self.assertIn("set", printing)

    @unittest.skipUnless(PRINTINGS_MAP, "No printings map loaded — skipping")
    def test_printings_map_has_multiple_printings(self):
        """At least some entries should have multiple printings (the whole point of dedup)."""
        multi = sum(1 for entry in PRINTINGS_MAP.values()
                    if len(entry.get("printings", [])) > 1)
        self.assertGreater(multi, 0,
                           "Expected at least some cards with multiple printings")


if __name__ == "__main__":
    unittest.main()
