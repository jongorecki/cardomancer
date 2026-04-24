# tests/enrichment/test_cull_token.py
# ---------------------------------------------------------------------------
# Tests for:
#   - web_enrichment.vanilla.is_vanilla_or_french_vanilla()
#   - query_parser: cull:true / cull:false token evaluation
#   - web_enrichment.repo.EnrichmentRepo.is_cull_candidate()
#
# Coverage:
#   - Vanilla / french-vanilla detection (5 spec examples + edge cases)
#   - cull:true → True when all 3 predicates hold
#   - cull:true → False when card has non-keyword oracle text (predicate 1 fails)
#   - cull:true → False when card is a staple (predicate 2 fails)
#   - cull:true → False when card is on CK buylist (predicate 3 fails)
#   - cull:false → negation of cull:true
#   - All 3 predicates fail independently
#   - No enrichment_data → conservative (assumes not a staple, not on buylist)
#   - EnrichmentRepo.is_cull_candidate() with DB-backed checks
#   - EnrichmentRepo.buylist_ck_price() fast read
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import enrichment_db
from web_enrichment.vanilla import is_vanilla_or_french_vanilla
from web_enrichment.repo import EnrichmentRepo
from query_parser import matches_query, parse_query, evaluate_query


# ---------------------------------------------------------------------------
# Fixture card data
# ---------------------------------------------------------------------------

# Grizzly Bears — vanilla (no text)
GRIZZLY_BEARS = {
    "name": "Grizzly Bears",
    "oracle_id": "bears-oid",
    "oracle_text": "",
    "keywords": [],
    "type_line": "Creature — Bear",
    "colors": ["G"],
    "cmc": 2.0,
    "prices": {"usd": "0.10"},
}

# Savannah Lions — vanilla (no text)
SAVANNAH_LIONS = {
    "name": "Savannah Lions",
    "oracle_id": "lions-oid",
    "oracle_text": "",
    "keywords": [],
    "type_line": "Creature — Cat",
    "colors": ["W"],
    "cmc": 1.0,
    "prices": {"usd": "0.15"},
}

# Serra Angel — french-vanilla (Flying, Vigilance)
SERRA_ANGEL = {
    "name": "Serra Angel",
    "oracle_id": "serra-oid",
    "oracle_text": "Flying, vigilance",
    "keywords": ["Flying", "Vigilance"],
    "type_line": "Creature — Angel",
    "colors": ["W"],
    "cmc": 5.0,
    "prices": {"usd": "0.20"},
}

# Lightning Bolt — complex text
LIGHTNING_BOLT = {
    "name": "Lightning Bolt",
    "oracle_id": "bolt-oid",
    "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    "keywords": [],
    "type_line": "Instant",
    "colors": ["R"],
    "cmc": 1.0,
    "prices": {"usd": "1.50"},
}

# Sol Ring — complex text
SOL_RING = {
    "name": "Sol Ring",
    "oracle_id": "solring-oid",
    "oracle_text": "{T}: Add {C}{C}.",
    "keywords": [],
    "type_line": "Artifact",
    "colors": [],
    "cmc": 1.0,
    "prices": {"usd": "2.00"},
}

# Nicol Bolas — complex text (legendary creature with complex ability)
NICOL_BOLAS = {
    "name": "Nicol Bolas",
    "oracle_id": "bolas-oid",
    "oracle_text": "Flying, trample\nAt the beginning of your upkeep, unless you pay {U}{B}{B}, sacrifice Nicol Bolas.",
    "keywords": ["Flying", "Trample"],
    "type_line": "Legendary Creature — Elder Dragon",
    "colors": ["U", "B", "R"],
    "cmc": 8.0,
    "prices": {"usd": "5.00"},
}

# Atraxa — complex text (has triggered ability)
ATRAXA = {
    "name": "Atraxa, Praetors' Voice",
    "oracle_id": "atraxa-oid",
    "oracle_text": "Flying, vigilance, deathtouch, lifelink\nAt the beginning of your end step, proliferate.",
    "keywords": ["Flying", "Vigilance", "Deathtouch", "Lifelink"],
    "type_line": "Legendary Creature — Phyrexian Angel Horror",
    "colors": ["W", "U", "B", "G"],
    "cmc": 4.0,
    "prices": {"usd": "25.00"},
}


# ---------------------------------------------------------------------------
# Vanilla / French-vanilla helper tests
# ---------------------------------------------------------------------------

class TestIsVanillaOrFrenchVanilla(unittest.TestCase):
    """Tests from the spec plus edge cases."""

    def test_grizzly_bears_vanilla(self):
        """Grizzly Bears: empty oracle_text → True."""
        self.assertTrue(is_vanilla_or_french_vanilla(""))

    def test_none_is_vanilla(self):
        """None oracle_text → True (treated as empty)."""
        self.assertTrue(is_vanilla_or_french_vanilla(None))

    def test_savannah_lions_vanilla(self):
        """Savannah Lions: empty oracle_text → True."""
        self.assertTrue(is_vanilla_or_french_vanilla(""))

    def test_serra_angel_french_vanilla_comma(self):
        """Serra Angel: 'Flying, vigilance' → True."""
        self.assertTrue(is_vanilla_or_french_vanilla("Flying, vigilance"))

    def test_flying_vigilance_newline(self):
        """Newline-separated keywords → True."""
        self.assertTrue(is_vanilla_or_french_vanilla("Flying\nVigilance"))

    def test_all_known_keywords(self):
        """All individually recognised keywords → True."""
        keywords = [
            "Flying", "Vigilance", "Trample", "Haste",
            "First strike", "Double strike", "Deathtouch",
            "Lifelink", "Menace", "Reach", "Hexproof",
            "Indestructible", "Defender", "Flash",
        ]
        for kw in keywords:
            with self.subTest(keyword=kw):
                self.assertTrue(is_vanilla_or_french_vanilla(kw))

    def test_lightning_bolt_not_vanilla(self):
        """Lightning Bolt deals 3 damage... → False."""
        self.assertFalse(
            is_vanilla_or_french_vanilla(
                "Lightning Bolt deals 3 damage to any target."
            )
        )

    def test_sol_ring_not_vanilla(self):
        """{T}: Add {C}{C}. → False (has mana symbol)."""
        self.assertFalse(is_vanilla_or_french_vanilla("{T}: Add {C}{C}."))

    def test_complex_triggered_ability(self):
        """Complex clause after keyword list → False."""
        self.assertFalse(
            is_vanilla_or_french_vanilla(
                "Flying, trample\n"
                "At the beginning of your upkeep, sacrifice this."
            )
        )

    def test_protection_not_french_vanilla(self):
        """'Protection from black' is a complex clause → False."""
        self.assertFalse(
            is_vanilla_or_french_vanilla("Protection from black")
        )

    def test_atraxa_not_french_vanilla(self):
        """Atraxa has a triggered ability on top of keywords → False."""
        self.assertFalse(is_vanilla_or_french_vanilla(ATRAXA["oracle_text"]))

    def test_multiple_vanilla_keywords(self):
        """Deathtouch, First strike → True."""
        self.assertTrue(is_vanilla_or_french_vanilla("Deathtouch, First strike"))

    def test_whitespace_only(self):
        """Whitespace-only oracle_text → True (treated as empty)."""
        self.assertTrue(is_vanilla_or_french_vanilla("   "))

    def test_mixed_case(self):
        """'flying, TRAMPLE' → True (case-insensitive match)."""
        self.assertTrue(is_vanilla_or_french_vanilla("flying, TRAMPLE"))

    def test_unknown_word_not_vanilla(self):
        """Unrecognised word → False even if single word."""
        self.assertFalse(is_vanilla_or_french_vanilla("Evade"))


# ---------------------------------------------------------------------------
# query_parser: cull:true / cull:false evaluation
# ---------------------------------------------------------------------------

class TestCullTokenNoEnrichment(unittest.TestCase):
    """cull:true/false evaluated without enrichment_data (no DB)."""

    def test_vanilla_card_is_cull_no_enrichment(self):
        """Vanilla + no enrichment_data → staple/buylist predicates default
        to False → cull:true returns True."""
        self.assertTrue(matches_query("cull:true", GRIZZLY_BEARS))

    def test_vanilla_cull_false_no_enrichment(self):
        """cull:false → False for a vanilla card with no enrichment data."""
        self.assertFalse(matches_query("cull:false", GRIZZLY_BEARS))

    def test_french_vanilla_is_cull(self):
        """French-vanilla card (Serra Angel) with no enrichment → cull:true."""
        self.assertTrue(matches_query("cull:true", SERRA_ANGEL))

    def test_complex_text_not_cull(self):
        """Lightning Bolt has complex oracle text → cull:true returns False."""
        self.assertFalse(matches_query("cull:true", LIGHTNING_BOLT))

    def test_complex_text_cull_false_true(self):
        """cull:false True for a card with complex text."""
        self.assertTrue(matches_query("cull:false", LIGHTNING_BOLT))

    def test_sol_ring_not_cull(self):
        """Sol Ring has {T}: ability → cull:true False."""
        self.assertFalse(matches_query("cull:true", SOL_RING))

    def test_atraxa_not_cull(self):
        """Atraxa has complex clause beyond keywords → cull:true False."""
        self.assertFalse(matches_query("cull:true", ATRAXA))

    def test_nicol_bolas_not_cull(self):
        """Nicol Bolas has triggered ability → cull:true False."""
        self.assertFalse(matches_query("cull:true", NICOL_BOLAS))

    def test_unknown_value_returns_false(self):
        """cull:maybe → False (only true/false/yes/no/0/1 accepted)."""
        self.assertFalse(matches_query("cull:maybe", GRIZZLY_BEARS))


class TestCullTokenWithEnrichment(unittest.TestCase):
    """cull:true/false with enrichment_data supplied."""

    def _enr(self, *, staple_universal=False, staple_cedh=False,
             staple_archetype=False, buylist_ck_price=None):
        return {
            "staple_universal": staple_universal,
            "staple_cedh": staple_cedh,
            "staple_archetype": staple_archetype,
            "salt": None,
            "in_combo": False,
            "buylist_ck_price": buylist_ck_price,
        }

    # --- All three predicates satisfied ---

    def test_vanilla_no_staple_no_buylist(self):
        """Grizzly Bears, no staple, no buylist → cull:true."""
        enr = self._enr()
        self.assertTrue(matches_query("cull:true", GRIZZLY_BEARS,
                                      enrichment_data=enr))

    # --- Predicate 1 fails: complex oracle text ---

    def test_complex_text_fails_predicate1(self):
        """Lightning Bolt oracle text → fails predicate 1."""
        enr = self._enr()
        self.assertFalse(matches_query("cull:true", LIGHTNING_BOLT,
                                       enrichment_data=enr))

    # --- Predicate 2 fails: card is a staple ---

    def test_staple_universal_fails_predicate2(self):
        """Vanilla card that is a universal staple → NOT a cull candidate."""
        enr = self._enr(staple_universal=True)
        self.assertFalse(matches_query("cull:true", GRIZZLY_BEARS,
                                       enrichment_data=enr))

    def test_staple_cedh_fails_predicate2(self):
        """Vanilla card that is a cEDH staple → NOT a cull candidate."""
        enr = self._enr(staple_cedh=True)
        self.assertFalse(matches_query("cull:true", GRIZZLY_BEARS,
                                       enrichment_data=enr))

    def test_staple_archetype_fails_predicate2(self):
        """Vanilla card that is an archetype staple → NOT a cull candidate."""
        enr = self._enr(staple_archetype=True)
        self.assertFalse(matches_query("cull:true", GRIZZLY_BEARS,
                                       enrichment_data=enr))

    # --- Predicate 3 fails: card is on CK buylist ---

    def test_buylist_nonzero_fails_predicate3(self):
        """Vanilla card on CK buylist (price $0.50) → NOT a cull candidate."""
        enr = self._enr(buylist_ck_price=0.50)
        self.assertFalse(matches_query("cull:true", GRIZZLY_BEARS,
                                       enrichment_data=enr))

    def test_buylist_zero_still_cull(self):
        """Buylist price of 0 (entry exists but at $0) → still a cull candidate."""
        enr = self._enr(buylist_ck_price=0.0)
        self.assertTrue(matches_query("cull:true", GRIZZLY_BEARS,
                                      enrichment_data=enr))

    def test_buylist_none_still_cull(self):
        """No buylist entry (None) → still a cull candidate."""
        enr = self._enr(buylist_ck_price=None)
        self.assertTrue(matches_query("cull:true", GRIZZLY_BEARS,
                                      enrichment_data=enr))

    # --- cull:false negation ---

    def test_cull_false_negates_true(self):
        """cull:false is the exact negation of cull:true."""
        enr = self._enr()
        vanilla_cull = matches_query("cull:true", GRIZZLY_BEARS,
                                     enrichment_data=enr)
        vanilla_not_cull = matches_query("cull:false", GRIZZLY_BEARS,
                                         enrichment_data=enr)
        self.assertTrue(vanilla_cull)
        self.assertFalse(vanilla_not_cull)

    def test_complex_card_cull_false_true(self):
        """A card with complex text: cull:false is True."""
        enr = self._enr()
        self.assertTrue(matches_query("cull:false", LIGHTNING_BOLT,
                                      enrichment_data=enr))

    # --- Combined with other tokens ---

    def test_cull_combined_with_color(self):
        """cull:true c:g — should match Grizzly Bears (green, vanilla)."""
        enr = self._enr()
        self.assertTrue(matches_query("cull:true c:g", GRIZZLY_BEARS,
                                      enrichment_data=enr))

    def test_cull_combined_with_type(self):
        """cull:true t:creature — matches Grizzly Bears but not Lightning Bolt."""
        enr = self._enr()
        self.assertTrue(matches_query("cull:true t:creature", GRIZZLY_BEARS,
                                      enrichment_data=enr))
        self.assertFalse(matches_query("cull:true t:creature", LIGHTNING_BOLT,
                                       enrichment_data=enr))

    def test_collect_enrichment_fields_includes_cull(self):
        """collect_enrichment_fields should include 'cull' when cull: is used."""
        from query_parser import collect_enrichment_fields
        ast = parse_query("cull:true")
        fields = collect_enrichment_fields(ast)
        self.assertIn("cull", fields)

    def test_collect_enrichment_fields_cull_in_compound(self):
        """'cull:true c:g' — collect_enrichment_fields still returns {'cull'}."""
        from query_parser import collect_enrichment_fields
        ast = parse_query("cull:true c:g")
        fields = collect_enrichment_fields(ast)
        self.assertIn("cull", fields)

    def test_tokenizer_recognises_cull(self):
        """Tokenizer should produce a FIELD_QUERY token for 'cull:true'."""
        from query_parser import tokenize
        tokens = tokenize("cull:true")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].type, "FIELD_QUERY")
        field, op, val = tokens[0].value
        self.assertEqual(field, "cull")
        self.assertEqual(val, "true")

    def test_tokenizer_recognises_cull_false(self):
        """Tokenizer should produce a FIELD_QUERY for 'cull:false'."""
        from query_parser import tokenize
        tokens = tokenize("cull:false")
        self.assertEqual(len(tokens), 1)
        field, op, val = tokens[0].value
        self.assertEqual(field, "cull")
        self.assertEqual(val, "false")


# ---------------------------------------------------------------------------
# EnrichmentRepo — buylist_ck_price() and is_cull_candidate()
# ---------------------------------------------------------------------------

class TestEnrichmentRepoCullMethods(unittest.TestCase):
    """Test the two new EnrichmentRepo methods against a real temp DB."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cull_repo_test_")
        self.db_path = os.path.join(self.tmpdir, "enrichment.db")
        self.conn = enrichment_db.get_connection(db_path=self.db_path)
        self.repo = EnrichmentRepo(db_path=self.db_path)

        # Seed card_universe rows
        enrichment_db.seed_card_universe(self.conn, [
            ("bears-oid", "Grizzly Bears"),
            ("lions-oid", "Savannah Lions"),
            ("bolt-oid", "Lightning Bolt"),
            ("solring-oid", "Sol Ring"),
            ("serra-oid", "Serra Angel"),
        ])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- buylist_ck_price --

    def test_buylist_ck_price_none_when_missing(self):
        """No row in buylists → None."""
        self.assertIsNone(self.repo.buylist_ck_price("bears-oid"))

    def test_buylist_ck_price_returns_price(self):
        """Row present → returns price_usd float."""
        self.conn.execute(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES (?, ?, ?, ?)",
            ("bears-oid", "ck", 0.25, "2026-01-01"),
        )
        self.conn.commit()
        price = self.repo.buylist_ck_price("bears-oid")
        self.assertAlmostEqual(price, 0.25)

    def test_buylist_ck_price_ignores_other_vendors(self):
        """Row for vendor='tcg' does not count."""
        self.conn.execute(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES (?, ?, ?, ?)",
            ("bolt-oid", "tcg", 1.50, "2026-01-01"),
        )
        self.conn.commit()
        self.assertIsNone(self.repo.buylist_ck_price("bolt-oid"))

    # -- is_cull_candidate --

    def test_vanilla_no_staple_no_buylist_is_cull(self):
        """Grizzly Bears with no DB rows → is_cull_candidate True."""
        self.assertTrue(
            self.repo.is_cull_candidate("bears-oid", oracle_text="")
        )

    def test_complex_text_not_cull(self):
        """Lightning Bolt oracle text fails predicate 1 → False."""
        self.assertFalse(
            self.repo.is_cull_candidate(
                "bolt-oid",
                oracle_text="Lightning Bolt deals 3 damage to any target."
            )
        )

    def test_french_vanilla_no_rows_is_cull(self):
        """Serra Angel ('Flying, vigilance') with no staple/buylist → True."""
        self.assertTrue(
            self.repo.is_cull_candidate("serra-oid",
                                        oracle_text="Flying, vigilance")
        )

    def test_staple_fails_predicate2(self):
        """Vanilla card with staple row → False."""
        self.conn.execute(
            "INSERT INTO staples (oracle_id, tier, source, score, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("bears-oid", "universal", "edhrec", 0.9, "2026-01-01"),
        )
        self.conn.commit()
        self.assertFalse(
            self.repo.is_cull_candidate("bears-oid", oracle_text="")
        )

    def test_buylist_positive_price_fails_predicate3(self):
        """Vanilla card with positive CK buylist price → False."""
        self.conn.execute(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES (?, ?, ?, ?)",
            ("lions-oid", "ck", 0.50, "2026-01-01"),
        )
        self.conn.commit()
        self.assertFalse(
            self.repo.is_cull_candidate("lions-oid", oracle_text="")
        )

    def test_buylist_zero_price_still_cull(self):
        """CK buylist price = 0 (not > 0) → still a cull candidate."""
        self.conn.execute(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES (?, ?, ?, ?)",
            ("lions-oid", "ck", 0.0, "2026-01-01"),
        )
        self.conn.commit()
        self.assertTrue(
            self.repo.is_cull_candidate("lions-oid", oracle_text="")
        )

    def test_all_predicates_fail_independently(self):
        """Three separate cards, each failing one predicate: all return False."""
        # Card A: complex text
        result_a = self.repo.is_cull_candidate(
            "bolt-oid",
            oracle_text="Lightning Bolt deals 3 damage to any target."
        )
        self.assertFalse(result_a, "Complex text card should not be cull")

        # Card B: vanilla but is a staple
        self.conn.execute(
            "INSERT INTO staples (oracle_id, tier, source, score, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("bears-oid", "archetype", "edhrec", 0.3, "2026-01-01"),
        )
        self.conn.commit()
        result_b = self.repo.is_cull_candidate("bears-oid", oracle_text="")
        self.assertFalse(result_b, "Staple card should not be cull")

        # Card C: vanilla but has CK buylist price
        self.conn.execute(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES (?, ?, ?, ?)",
            ("lions-oid", "ck", 0.25, "2026-01-01"),
        )
        self.conn.commit()
        result_c = self.repo.is_cull_candidate("lions-oid", oracle_text="")
        self.assertFalse(result_c, "Buylist card should not be cull")

    def test_idempotent_multiple_calls(self):
        """Calling is_cull_candidate twice gives the same result."""
        r1 = self.repo.is_cull_candidate("bears-oid", oracle_text="")
        r2 = self.repo.is_cull_candidate("bears-oid", oracle_text="")
        self.assertEqual(r1, r2)

    def test_db_path_redirect_pattern(self):
        """DB_PATH global redirect pattern (per memory note) works correctly."""
        import enrichment_db as edb
        orig = edb.DB_PATH
        edb.DB_PATH = self.db_path
        try:
            repo = EnrichmentRepo()
            result = repo.is_cull_candidate("bears-oid", oracle_text="")
            self.assertTrue(result)
        finally:
            edb.DB_PATH = orig


if __name__ == "__main__":
    unittest.main()
