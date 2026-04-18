# test_sort_config.py
# Unit tests for sort configuration loading, bin assignment, overflow, and limits.

import unittest
from unittest.mock import patch

from sort_config import SortConfig
from query_parser import collect_otag_terms


# ---------------------------------------------------------------------------
# Sample card data (reused from test_query_parser)
# ---------------------------------------------------------------------------

LIGHTNING_BOLT = {
    "name": "Lightning Bolt",
    "set": "m11",
    "colors": ["R"],
    "color_identity": ["R"],
    "cmc": 1.0,
    "type_line": "Instant",
    "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    "rarity": "common",
    "prices": {"usd": "1.50"},
    "keywords": [],
    "legalities": {"modern": "legal", "commander": "legal"},
    "produced_mana": [],
    "oracle_id": "bolt-001",
}

BIRDS_OF_PARADISE = {
    "name": "Birds of Paradise",
    "set": "m12",
    "colors": ["G"],
    "color_identity": ["G"],
    "cmc": 1.0,
    "type_line": "Creature — Bird",
    "oracle_text": "{T}: Add one mana of any color.",
    "rarity": "rare",
    "prices": {"usd": "8.00"},
    "keywords": ["Flying"],
    "legalities": {"modern": "legal", "commander": "legal"},
    "produced_mana": ["W", "U", "B", "R", "G"],
    "oracle_id": "birds-002",
}

NICOL_BOLAS = {
    "name": "Nicol Bolas, the Ravager",
    "set": "m19",
    "colors": ["U", "B", "R"],
    "color_identity": ["U", "B", "R"],
    "cmc": 4.0,
    "type_line": "Legendary Creature — Elder Dragon",
    "rarity": "mythic",
    "prices": {"usd": "15.00"},
    "keywords": ["Flying"],
    "legalities": {"modern": "legal", "commander": "legal"},
    "oracle_id": "bolas-003",
}

FOREST = {
    "name": "Forest",
    "set": "m21",
    "colors": [],
    "color_identity": ["G"],
    "cmc": 0.0,
    "type_line": "Basic Land — Forest",
    "rarity": "common",
    "prices": {"usd": "0.10"},
    "keywords": [],
    "legalities": {"modern": "legal", "commander": "legal"},
    "produced_mana": ["G"],
    "oracle_id": "forest-004",
}

WRATH_OF_GOD = {
    "name": "Wrath of God",
    "set": "2xm",
    "colors": ["W"],
    "color_identity": ["W"],
    "cmc": 4.0,
    "type_line": "Sorcery",
    "rarity": "rare",
    "prices": {"usd": "5.25"},
    "keywords": [],
    "legalities": {"modern": "legal", "commander": "legal"},
    "oracle_id": "wrath-005",
}

SOL_RING = {
    "name": "Sol Ring",
    "set": "c21",
    "colors": [],
    "color_identity": [],
    "cmc": 1.0,
    "type_line": "Artifact",
    "rarity": "uncommon",
    "prices": {"usd": "2.50"},
    "keywords": [],
    "legalities": {"commander": "legal"},
    "oracle_id": "solring-006",
}


# ---------------------------------------------------------------------------
# Config Loading Tests
# ---------------------------------------------------------------------------

class TestFromLines(unittest.TestCase):

    def test_basic_config(self):
        lines = [
            "bins: 4",
            "fallback: 4",
            "bin1: c:w",
            "bin2: c:u",
            "bin3: c:r",
        ]
        config = SortConfig.from_lines(lines)
        self.assertEqual(config.bin_count, 4)
        self.assertEqual(config.fallback_bin, 4)
        self.assertEqual(len(config.bin_queries), 3)
        self.assertIsNone(config.bin_limit)

    def test_config_with_limit(self):
        lines = [
            "bins: 4",
            "fallback: 4",
            "limit: 50",
            "bin1: c:w",
            "bin2: c:u",
        ]
        config = SortConfig.from_lines(lines)
        self.assertEqual(config.bin_limit, 50)

    def test_comments_and_blanks(self):
        lines = [
            "# This is a comment",
            "",
            "bins: 3",
            "# Another comment",
            "fallback: 3",
            "",
            "bin1: t:creature",
            "bin2: t:land",
            "# bin3 is fallback",
        ]
        config = SortConfig.from_lines(lines)
        self.assertEqual(config.bin_count, 3)
        self.assertEqual(len(config.bin_queries), 2)

    def test_missing_bins_raises(self):
        lines = [
            "fallback: 3",
            "bin1: c:w",
        ]
        with self.assertRaises(ValueError) as ctx:
            SortConfig.from_lines(lines)
        self.assertIn("bins:", str(ctx.exception))

    def test_missing_fallback_raises(self):
        lines = [
            "bins: 3",
            "bin1: c:w",
        ]
        with self.assertRaises(ValueError) as ctx:
            SortConfig.from_lines(lines)
        self.assertIn("fallback:", str(ctx.exception))

    def test_fallback_out_of_range_raises(self):
        lines = [
            "bins: 3",
            "fallback: 5",
            "bin1: c:w",
        ]
        with self.assertRaises(ValueError):
            SortConfig.from_lines(lines)

    def test_bin_out_of_range_raises(self):
        lines = [
            "bins: 3",
            "fallback: 3",
            "bin5: c:w",
        ]
        with self.assertRaises(ValueError):
            SortConfig.from_lines(lines)

    def test_invalid_query_raises(self):
        lines = [
            "bins: 3",
            "fallback: 3",
            "bin1: zzz:invalid",
        ]
        with self.assertRaises(ValueError) as ctx:
            SortConfig.from_lines(lines)
        self.assertIn("Invalid query", str(ctx.exception))

    def test_unrecognized_line_raises(self):
        lines = [
            "bins: 3",
            "fallback: 3",
            "something random",
        ]
        with self.assertRaises(ValueError) as ctx:
            SortConfig.from_lines(lines)
        self.assertIn("Unrecognized", str(ctx.exception))

    def test_invalid_limit_raises(self):
        lines = [
            "bins: 3",
            "fallback: 3",
            "limit: -5",
        ]
        with self.assertRaises(ValueError):
            SortConfig.from_lines(lines)

    def test_case_insensitive_directives(self):
        lines = [
            "BINS: 3",
            "Fallback: 3",
            "Bin1: c:w",
        ]
        config = SortConfig.from_lines(lines)
        self.assertEqual(config.bin_count, 3)
        self.assertEqual(config.fallback_bin, 3)


# ---------------------------------------------------------------------------
# Bin Assignment Tests
# ---------------------------------------------------------------------------

class TestGetBin(unittest.TestCase):

    def setUp(self):
        lines = [
            "bins: 5",
            "fallback: 5",
            "bin1: usd>=10",
            "bin2: usd>=5 usd<10",
            "bin3: t:creature",
            "bin4: t:land",
        ]
        self.config = SortConfig.from_lines(lines)

    def test_first_match_wins(self):
        """Nicol Bolas is $15 AND a creature — should hit bin 1 (price first)."""
        self.assertEqual(self.config.get_bin(NICOL_BOLAS), 1)

    def test_price_tier_2(self):
        """Birds is $8 — hits bin 2."""
        self.assertEqual(self.config.get_bin(BIRDS_OF_PARADISE), 2)

    def test_creature_fallthrough(self):
        """Lightning Bolt is $1.50 instant — no match until fallback."""
        self.config.reset_counts()
        self.assertEqual(self.config.get_bin(LIGHTNING_BOLT), 5)

    def test_land(self):
        """Forest is a land — hits bin 4."""
        self.assertEqual(self.config.get_bin(FOREST), 4)

    def test_fallback_for_none(self):
        """None card data goes to fallback."""
        self.assertEqual(self.config.get_bin(None), 5)

    def test_fallback_for_empty(self):
        """Empty card data goes to fallback."""
        self.assertEqual(self.config.get_bin({}), 5)


# ---------------------------------------------------------------------------
# Overflow / Bin Limit Tests
# ---------------------------------------------------------------------------

class TestOverflow(unittest.TestCase):

    def test_overflow_to_next_matching_bin(self):
        """When bin hits limit, cards overflow to next bin with same query."""
        lines = [
            "bins: 4",
            "fallback: 4",
            "limit: 2",
            "bin1: usd<1",
            "bin2: usd<1",
            "bin3: usd<1",
        ]
        config = SortConfig.from_lines(lines)

        # First 2 cheap cards go to bin 1
        self.assertEqual(config.get_bin(FOREST), 1)
        self.assertEqual(config.get_bin(FOREST), 1)

        # Next 2 overflow to bin 2
        self.assertEqual(config.get_bin(FOREST), 2)
        self.assertEqual(config.get_bin(FOREST), 2)

        # Next 2 overflow to bin 3
        self.assertEqual(config.get_bin(FOREST), 3)
        self.assertEqual(config.get_bin(FOREST), 3)

        # All overflow bins full — goes to fallback
        self.assertEqual(config.get_bin(FOREST), 4)

    def test_no_overflow_without_limit(self):
        """Without a limit, all matching cards go to the first matching bin."""
        lines = [
            "bins: 3",
            "fallback: 3",
            "bin1: usd<1",
            "bin2: usd<1",
        ]
        config = SortConfig.from_lines(lines)

        for _ in range(10):
            self.assertEqual(config.get_bin(FOREST), 1)

    def test_mixed_queries_with_limit(self):
        """Different query bins fill independently."""
        lines = [
            "bins: 4",
            "fallback: 4",
            "limit: 1",
            "bin1: t:creature",
            "bin2: t:instant",
            "bin3: t:land",
        ]
        config = SortConfig.from_lines(lines)

        # Each bin holds 1 card
        self.assertEqual(config.get_bin(BIRDS_OF_PARADISE), 1)  # creature → bin 1
        self.assertEqual(config.get_bin(LIGHTNING_BOLT), 2)      # instant → bin 2
        self.assertEqual(config.get_bin(FOREST), 3)              # land → bin 3

        # Bin 1 full, next creature goes to fallback (no overflow bin)
        self.assertEqual(config.get_bin(NICOL_BOLAS), 4)

    def test_reset_counts(self):
        lines = [
            "bins: 3",
            "fallback: 3",
            "limit: 1",
            "bin1: t:creature",
        ]
        config = SortConfig.from_lines(lines)

        self.assertEqual(config.get_bin(BIRDS_OF_PARADISE), 1)
        self.assertEqual(config.get_bin(NICOL_BOLAS), 3)  # bin 1 full

        config.reset_counts()

        self.assertEqual(config.get_bin(NICOL_BOLAS), 1)  # back to bin 1


# ---------------------------------------------------------------------------
# Describe / Status Tests
# ---------------------------------------------------------------------------

class TestDescribeStatus(unittest.TestCase):

    def setUp(self):
        lines = [
            "bins: 3",
            "fallback: 3",
            "bin1: c:w",
            "bin2: t:creature",
        ]
        self.config = SortConfig.from_lines(lines)

    def test_describe(self):
        desc = self.config.describe()
        self.assertIn("3 bins", desc)
        self.assertIn("fallback=bin 3", desc)
        self.assertIn("Bin 1: c:w", desc)
        self.assertIn("Bin 2: t:creature", desc)

    def test_describe_with_limit(self):
        lines = [
            "bins: 3",
            "fallback: 3",
            "limit: 50",
            "bin1: c:w",
        ]
        config = SortConfig.from_lines(lines)
        desc = config.describe()
        self.assertIn("limit=50", desc)

    def test_status_counts(self):
        self.config.get_bin(WRATH_OF_GOD)     # white → bin 1
        self.config.get_bin(BIRDS_OF_PARADISE)  # creature → bin 2
        self.config.get_bin(LIGHTNING_BOLT)     # fallback → bin 3

        status = self.config.get_status()
        self.assertIn("Bin 1: 1", status)
        self.assertIn("Bin 2: 1", status)
        self.assertIn("Bin 3 (fallback): 1", status)

    def test_unused_bins_shown_in_describe(self):
        lines = [
            "bins: 5",
            "fallback: 5",
            "bin1: c:w",
            "bin3: c:r",
        ]
        config = SortConfig.from_lines(lines)
        desc = config.describe()
        self.assertIn("Unused bins", desc)


# ---------------------------------------------------------------------------
# Otag Collection in Config
# ---------------------------------------------------------------------------

class TestOtagCollection(unittest.TestCase):

    @patch('sort_config.fetch_otag_data')
    def test_otag_prefetch(self, mock_fetch):
        """Config with otag queries should trigger pre-fetching."""
        mock_fetch.return_value = {
            "ramp": {"birds-002", "solring-006"},
            "removal": {"bolt-001"},
        }
        lines = [
            "bins: 3",
            "fallback: 3",
            "bin1: otag:ramp",
            "bin2: otag:removal",
        ]
        config = SortConfig.from_lines(lines)

        mock_fetch.assert_called_once()
        called_tags = mock_fetch.call_args[0][0]
        self.assertIn("ramp", called_tags)
        self.assertIn("removal", called_tags)

        # Verify the cache is set
        self.assertIn("ramp", config.otag_cache)
        self.assertIn("removal", config.otag_cache)

    def test_no_otag_no_fetch(self):
        """Config without otag queries should not call fetch."""
        lines = [
            "bins: 3",
            "fallback: 3",
            "bin1: c:w",
            "bin2: t:creature",
        ]
        # This should not attempt any network calls
        config = SortConfig.from_lines(lines)
        self.assertEqual(config.otag_cache, {})


# ---------------------------------------------------------------------------
# File Loading Tests
# ---------------------------------------------------------------------------

class TestFromFile(unittest.TestCase):

    def test_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            SortConfig.from_file("nonexistent_file_12345.txt")

    def test_load_price_tiers(self):
        """Load the actual price_tiers.txt config file."""
        import os
        config_path = os.path.join(
            os.path.dirname(__file__), "sort_configs", "price_tiers.txt"
        )
        if not os.path.exists(config_path):
            self.skipTest("price_tiers.txt not found")

        config = SortConfig.from_file(config_path)
        self.assertEqual(config.bin_count, 8)
        self.assertEqual(config.fallback_bin, 8)
        self.assertGreater(len(config.bin_queries), 0)

        # Nicol Bolas ($15) should go to bin 1 (usd>=10)
        self.assertEqual(config.get_bin(NICOL_BOLAS), 1)
        # Birds ($8) should go to bin 2 (usd>=5 usd<10)
        self.assertEqual(config.get_bin(BIRDS_OF_PARADISE), 2)
        # Forest ($0.10) should go to bin 4 (first usd<1 bin)
        self.assertEqual(config.get_bin(FOREST), 4)


# ---------------------------------------------------------------------------
# Sort Config with otag-based Bin Assignment
# ---------------------------------------------------------------------------

class TestOtagBinAssignment(unittest.TestCase):

    def test_otag_sorting(self):
        """Test bin assignment using otag cache."""
        from query_parser import parse_query

        otag_cache = {
            "ramp": {"birds-002", "solring-006"},
            "removal": {"bolt-001", "wrath-005"},
        }

        bin_queries = [
            (1, "otag:ramp", parse_query("otag:ramp")),
            (2, "otag:removal", parse_query("otag:removal")),
        ]

        config = SortConfig(
            bin_count=3,
            fallback_bin=3,
            bin_queries=bin_queries,
            otag_cache=otag_cache,
        )

        self.assertEqual(config.get_bin(BIRDS_OF_PARADISE), 1)
        self.assertEqual(config.get_bin(SOL_RING), 1)
        self.assertEqual(config.get_bin(LIGHTNING_BOLT), 2)
        self.assertEqual(config.get_bin(WRATH_OF_GOD), 2)
        self.assertEqual(config.get_bin(FOREST), 3)  # fallback


if __name__ == '__main__':
    unittest.main()
