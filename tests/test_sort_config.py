"""Unit tests for sort_config.py — config loading, bin assignment, overflow."""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sort_config import SortConfig
from query_parser import parse_query


# Sample card data
RED_CREATURE = {
    "name": "Goblin Guide",
    "colors": ["R"],
    "color_identity": ["R"],
    "cmc": 1.0,
    "type_line": "Creature — Goblin",
    "set": "zen",
    "rarity": "rare",
    "oracle_text": "Haste",
    "keywords": ["Haste"],
    "prices": {"usd": "5.00"},
    "power": "2",
    "toughness": "2",
    "legalities": {"modern": "legal", "commander": "legal"},
    "produced_mana": [],
    "set_type": "expansion",
    "oracle_id": "goblin-guide-oid",
}

WHITE_CREATURE = {
    "name": "Savannah Lions",
    "colors": ["W"],
    "color_identity": ["W"],
    "cmc": 1.0,
    "type_line": "Creature — Cat",
    "set": "9ed",
    "rarity": "rare",
    "oracle_text": "",
    "keywords": [],
    "prices": {"usd": "0.50"},
    "power": "2",
    "toughness": "1",
    "legalities": {"modern": "legal", "commander": "legal"},
    "produced_mana": [],
    "set_type": "core",
    "oracle_id": "savannah-lions-oid",
}

BLUE_INSTANT = {
    "name": "Counterspell",
    "colors": ["U"],
    "color_identity": ["U"],
    "cmc": 2.0,
    "type_line": "Instant",
    "set": "ice",
    "rarity": "common",
    "oracle_text": "Counter target spell.",
    "keywords": [],
    "prices": {"usd": "1.00"},
    "power": None,
    "toughness": None,
    "legalities": {"modern": "not_legal", "commander": "legal"},
    "produced_mana": [],
    "set_type": "expansion",
    "oracle_id": "counterspell-oid",
}

ARTIFACT = {
    "name": "Sol Ring",
    "colors": [],
    "color_identity": [],
    "cmc": 1.0,
    "type_line": "Artifact",
    "set": "c21",
    "rarity": "uncommon",
    "oracle_text": "{T}: Add {C}{C}.",
    "keywords": [],
    "prices": {"usd": "2.00"},
    "power": None,
    "toughness": None,
    "legalities": {"modern": "not_legal", "commander": "legal"},
    "produced_mana": ["C"],
    "set_type": "commander",
    "oracle_id": "sol-ring-oid",
}


class TestFromLines(unittest.TestCase):

    def test_basic_config(self):
        lines = [
            "bins: 3",
            "fallback: 3",
            "bin1: c:w",
            "bin2: c:r",
        ]
        config = SortConfig.from_lines(lines)
        self.assertEqual(config.bin_count, 3)
        self.assertEqual(config.fallback_bin, 3)
        self.assertEqual(len(config.bin_queries), 2)

    def test_with_limit(self):
        lines = [
            "bins: 4",
            "fallback: 4",
            "limit: 50",
            "bin1: c:w",
            "bin2: c:r",
            "bin3: c:u",
        ]
        config = SortConfig.from_lines(lines)
        self.assertEqual(config.bin_limit, 50)

    def test_comments_and_blanks(self):
        lines = [
            "# Sort by color",
            "",
            "bins: 2",
            "fallback: 2",
            "# White creatures go in bin 1",
            "bin1: c:w t:creature",
        ]
        config = SortConfig.from_lines(lines)
        self.assertEqual(config.bin_count, 2)
        self.assertEqual(len(config.bin_queries), 1)

    def test_missing_bins_directive(self):
        lines = ["fallback: 1", "bin1: c:w"]
        with self.assertRaises(ValueError):
            SortConfig.from_lines(lines)

    def test_missing_fallback_directive(self):
        lines = ["bins: 2", "bin1: c:w"]
        with self.assertRaises(ValueError):
            SortConfig.from_lines(lines)

    def test_bin_out_of_range(self):
        lines = ["bins: 2", "fallback: 2", "bin5: c:w"]
        with self.assertRaises(ValueError):
            SortConfig.from_lines(lines)

    def test_fallback_out_of_range(self):
        lines = ["bins: 2", "fallback: 5", "bin1: c:w"]
        with self.assertRaises(ValueError):
            SortConfig.from_lines(lines)

    def test_invalid_query(self):
        lines = ["bins: 2", "fallback: 2", "bin1: xyz:foo"]
        with self.assertRaises(ValueError):
            SortConfig.from_lines(lines)

    def test_invalid_limit(self):
        lines = ["bins: 2", "fallback: 2", "limit: -5", "bin1: c:w"]
        with self.assertRaises(ValueError):
            SortConfig.from_lines(lines)

    def test_unrecognized_line(self):
        lines = ["bins: 2", "fallback: 2", "garbage line here"]
        with self.assertRaises(ValueError):
            SortConfig.from_lines(lines)


class TestGetBin(unittest.TestCase):

    def _make_config(self, bin_queries_raw, bin_count=4, fallback=4, limit=None):
        """Helper to build a SortConfig from (bin_num, query_str) pairs."""
        parsed = []
        for bin_num, query_str in bin_queries_raw:
            ast = parse_query(query_str)
            parsed.append((bin_num, query_str, ast))
        return SortConfig(bin_count, fallback, parsed, bin_limit=limit)

    def test_basic_bin_assignment(self):
        config = self._make_config([
            (1, "c:w"),
            (2, "c:r"),
            (3, "c:u"),
        ])
        self.assertEqual(config.get_bin(WHITE_CREATURE), 1)
        self.assertEqual(config.get_bin(RED_CREATURE), 2)
        self.assertEqual(config.get_bin(BLUE_INSTANT), 3)
        self.assertEqual(config.get_bin(ARTIFACT), 4)  # fallback

    def test_first_match_wins(self):
        """Both bins match, but first one should be returned."""
        config = self._make_config([
            (1, "t:creature"),
            (2, "c:r"),
        ])
        self.assertEqual(config.get_bin(RED_CREATURE), 1)

    def test_fallback_for_no_match(self):
        config = self._make_config([(1, "c:w")])
        self.assertEqual(config.get_bin(BLUE_INSTANT), 4)

    def test_none_card_data(self):
        config = self._make_config([(1, "c:w")])
        self.assertEqual(config.get_bin(None), 4)

    def test_empty_card_data(self):
        config = self._make_config([(1, "c:w")])
        self.assertEqual(config.get_bin({}), 4)

    def test_counts_tracked(self):
        config = self._make_config([(1, "c:r")])
        config.get_bin(RED_CREATURE)
        config.get_bin(RED_CREATURE)
        self.assertEqual(config.bin_card_counts.get(1, 0), 2)

    def test_fallback_counted(self):
        config = self._make_config([(1, "c:w")])
        config.get_bin(ARTIFACT)
        self.assertEqual(config.bin_card_counts.get(4, 0), 1)

    def test_reset_counts(self):
        config = self._make_config([(1, "c:r")])
        config.get_bin(RED_CREATURE)
        config.reset_counts()
        self.assertEqual(config.bin_card_counts, {})


class TestBinOverflow(unittest.TestCase):

    def _make_config(self, bin_queries_raw, bin_count=4, fallback=4, limit=None):
        parsed = []
        for bin_num, query_str in bin_queries_raw:
            ast = parse_query(query_str)
            parsed.append((bin_num, query_str, ast))
        return SortConfig(bin_count, fallback, parsed, bin_limit=limit)

    def test_overflow_to_next_matching_bin(self):
        """When bin 1 is full, cards overflow to bin 2 (same query)."""
        config = self._make_config([
            (1, "c:r"),
            (2, "c:r"),
        ], limit=2)

        # Fill bin 1
        self.assertEqual(config.get_bin(RED_CREATURE), 1)
        self.assertEqual(config.get_bin(RED_CREATURE), 1)
        # Bin 1 full, overflow to bin 2
        self.assertEqual(config.get_bin(RED_CREATURE), 2)
        self.assertEqual(config.get_bin(RED_CREATURE), 2)
        # Bin 2 full too, fallback
        self.assertEqual(config.get_bin(RED_CREATURE), 4)

    def test_no_overflow_without_limit(self):
        config = self._make_config([
            (1, "c:r"),
            (2, "c:r"),
        ], limit=None)

        for _ in range(100):
            self.assertEqual(config.get_bin(RED_CREATURE), 1)

    def test_mixed_overflow(self):
        """Bins with different queries — only matching bins overflow."""
        config = self._make_config([
            (1, "c:w"),
            (2, "c:r"),
            (3, "c:r"),
        ], limit=1)

        self.assertEqual(config.get_bin(RED_CREATURE), 2)
        # bin 2 full, overflow to bin 3
        self.assertEqual(config.get_bin(RED_CREATURE), 3)
        # bin 3 full, fallback
        self.assertEqual(config.get_bin(RED_CREATURE), 4)


class TestDescribeAndStatus(unittest.TestCase):

    def test_describe(self):
        lines = ["bins: 3", "fallback: 3", "bin1: c:w", "bin2: c:r"]
        config = SortConfig.from_lines(lines)
        desc = config.describe()
        self.assertIn("3 bins", desc)
        self.assertIn("fallback=bin 3", desc)
        self.assertIn("Bin 1:", desc)
        self.assertIn("c:w", desc)

    def test_status(self):
        lines = ["bins: 3", "fallback: 3", "bin1: c:w", "bin2: c:r"]
        config = SortConfig.from_lines(lines)
        config.get_bin(WHITE_CREATURE)
        status = config.get_status()
        self.assertIn("Bin 1: 1", status)
        self.assertIn("Bin 2: 0", status)


if __name__ == "__main__":
    unittest.main()
