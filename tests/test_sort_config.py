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


class TestSortConfigEnrichment(unittest.TestCase):
    """Test enrichment token detection and per-card lookup in SortConfig."""

    def _make_config(self, query_str, fallback=2):
        ast = parse_query(query_str)
        return SortConfig(
            bin_count=2,
            fallback_bin=fallback,
            bin_queries=[(1, query_str, ast)],
        )

    def test_needs_enrichment_flag_set_for_staple(self):
        config = SortConfig.from_lines([
            "bins: 2", "fallback: 2", "bin1: staple:universal",
        ])
        self.assertTrue(config._needs_enrichment)

    def test_needs_enrichment_flag_not_set_for_color(self):
        config = SortConfig.from_lines([
            "bins: 2", "fallback: 2", "bin1: c:r",
        ])
        self.assertFalse(config._needs_enrichment)

    def test_needs_enrichment_flag_set_for_salt(self):
        config = SortConfig.from_lines([
            "bins: 2", "fallback: 2", "bin1: salt>2",
        ])
        self.assertTrue(config._needs_enrichment)

    def test_needs_enrichment_flag_set_for_combo(self):
        config = SortConfig.from_lines([
            "bins: 2", "fallback: 2", "bin1: combo:true",
        ])
        self.assertTrue(config._needs_enrichment)

    def test_staple_query_routes_correctly_with_enrichment_data(self):
        """Cards with staple data route to bin 1; others fall back."""
        from unittest.mock import patch

        config = SortConfig.from_lines([
            "bins: 2", "fallback: 2", "bin1: staple:universal",
        ])

        staple_data = {
            "staple_universal": True, "staple_cedh": False,
            "staple_archetype": False, "salt": 1.2, "in_combo": False,
        }
        non_staple_data = {
            "staple_universal": False, "staple_cedh": False,
            "staple_archetype": False, "salt": 0.1, "in_combo": False,
        }

        card_staple = {"oracle_id": "oid-staple", "name": "Sol Ring",
                       "colors": [], "cmc": 1.0, "type_line": "Artifact",
                       "rarity": "uncommon", "prices": {}}
        card_junk = {"oracle_id": "oid-junk", "name": "Squire",
                     "colors": ["W"], "cmc": 1.0, "type_line": "Creature",
                     "rarity": "common", "prices": {}}

        def fake_enr(oracle_id):
            return staple_data if oracle_id == "oid-staple" else non_staple_data

        with patch.object(config, "_get_enrichment_data", side_effect=fake_enr):
            self.assertEqual(config.get_bin(card_staple), 1)
            self.assertEqual(config.get_bin(card_junk), 2)

    def test_enrichment_cache_used_on_second_call(self):
        """Same oracle_id is only looked up once (cached)."""
        from unittest.mock import patch

        config = SortConfig.from_lines([
            "bins: 2", "fallback: 2", "bin1: staple:universal",
        ])
        config._enr_cache["oid-cached"] = {
            "staple_universal": True, "staple_cedh": False,
            "staple_archetype": False, "salt": None, "in_combo": False,
        }
        card = {"oracle_id": "oid-cached", "name": "Test", "colors": [],
                "cmc": 0.0, "type_line": "Artifact", "rarity": "common",
                "prices": {}}

        with patch.object(config, "_get_enrichment_data",
                          wraps=config._get_enrichment_data) as mock_lookup:
            config.get_bin(card)
            # Cache hit — real method not called for DB part
            self.assertNotIn("oid-cached", [
                c.args[0] for c in mock_lookup.call_args_list
                if c.args[0] not in config._enr_cache
            ])

    def test_no_enrichment_data_without_flag(self):
        """Without enrichment tokens, enrichment_data stays None (no DB hit)."""
        from unittest.mock import patch

        config = SortConfig.from_lines([
            "bins: 2", "fallback: 2", "bin1: c:r",
        ])
        self.assertFalse(config._needs_enrichment)

        card = {"oracle_id": "oid-x", "name": "Goblin", "colors": ["R"],
                "cmc": 1.0, "type_line": "Creature", "rarity": "common",
                "prices": {}}

        with patch.object(config, "_get_enrichment_data") as mock_lookup:
            config.get_bin(card)
            mock_lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
