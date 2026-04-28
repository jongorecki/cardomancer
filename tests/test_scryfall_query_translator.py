"""Tests for web_enrichment/scryfall_query_translator.py.

Pinned-output tests for every supported predicate, plus AND/OR/NOT
combinations and dropped-clause reporting. If any of these change,
update deliberately — downstream callers (sort-config bin preview,
Query Helper "view on Scryfall" link) depend on the exact strings.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_enrichment.scryfall_query_translator import (
    to_scryfall, to_scryfall_url
)


class TestDirectFieldMappings(unittest.TestCase):
    """Each row is one DSL predicate that maps cleanly to Scryfall."""

    def assertTranslates(self, dsl, expected_scryfall, expected_dropped=None):
        q, dropped = to_scryfall(dsl)
        self.assertEqual(q, expected_scryfall,
                         f"\nDSL:      {dsl}\nGot:      {q}\nExpected: {expected_scryfall}")
        self.assertEqual(dropped, expected_dropped or [])

    def test_color(self):
        self.assertTranslates("c:rg", "c:rg")
        self.assertTranslates("color:white", "c:white")

    def test_color_identity(self):
        self.assertTranslates("ci:gw", "id:gw")
        self.assertTranslates("identity:rug", "id:rug")

    def test_type(self):
        self.assertTranslates("t:creature", "t:creature")
        self.assertTranslates("type:legend", "t:legend")

    def test_mana_value(self):
        self.assertTranslates("cmc<=3", "cmc<=3")
        self.assertTranslates("mv>=4", "cmc>=4")

    def test_set(self):
        self.assertTranslates("s:cmr", "s:cmr")
        self.assertTranslates("e:lea", "s:lea")

    def test_rarity(self):
        self.assertTranslates("r:mythic", "r:mythic")
        self.assertTranslates("r>=rare", "r>=rare")

    def test_price(self):
        self.assertTranslates("usd>=1", "usd>=1")
        self.assertTranslates("price<5", "usd<5")
        self.assertTranslates("usd=0.50", "usd=0.50")

    def test_power_toughness(self):
        self.assertTranslates("pow>=4", "pow>=4")
        self.assertTranslates("tou<=2", "tou<=2")

    def test_oracle(self):
        self.assertTranslates("o:flying", "o:flying")
        self.assertTranslates("oracle:trample", "o:trample")

    def test_oracle_with_quoted_phrase(self):
        # Multi-word values get quoted on Scryfall too.
        self.assertTranslates('o:"draw a card"', 'o:"draw a card"')

    def test_name_substring(self):
        self.assertTranslates("name:bolt", "name:bolt")

    def test_keyword(self):
        self.assertTranslates("kw:flying", "keyword:flying")
        self.assertTranslates("keyword:trample", "keyword:trample")

    def test_otag_passes_through(self):
        # otag is the highest-leverage cross-platform predicate
        self.assertTranslates("otag:ramp", "otag:ramp")
        self.assertTranslates("otag:wrath-effect", "otag:wrath-effect")

    def test_legal(self):
        self.assertTranslates("f:commander", "f:commander")
        self.assertTranslates("legal:modern", "f:modern")

    def test_produces(self):
        self.assertTranslates("produces:c", "produces:c")

    def test_set_type(self):
        self.assertTranslates("st:expansion", "st:expansion")
        self.assertTranslates("settype:masters", "st:masters")


class TestIsPredicate(unittest.TestCase):
    """is:X passes through except for scan-time predicates."""

    def test_is_legendary(self):
        q, dropped = to_scryfall("is:legendary")
        self.assertEqual(q, "is:legendary")
        self.assertEqual(dropped, [])

    def test_is_creature_passes_through(self):
        # Scryfall accepts both is:creature and t:creature; we just pass through.
        q, dropped = to_scryfall("is:creature")
        self.assertEqual(q, "is:creature")
        self.assertEqual(dropped, [])

    def test_is_foilscan_dropped(self):
        q, dropped = to_scryfall("is:foilscan")
        self.assertEqual(q, "")
        self.assertEqual(len(dropped), 1)
        self.assertIn("is:foilscan", dropped[0])
        self.assertIn("scan-time", dropped[0])

    def test_is_detected_foil_dropped(self):
        q, dropped = to_scryfall("is:detected_foil")
        self.assertEqual(q, "")
        self.assertEqual(len(dropped), 1)
        self.assertIn("scan-time", dropped[0])


class TestEnrichmentDropping(unittest.TestCase):
    """Enrichment-only predicates drop with a labeled reason."""

    def _assert_dropped(self, dsl, field_in_message):
        q, dropped = to_scryfall(dsl)
        self.assertEqual(q, "")
        self.assertEqual(len(dropped), 1)
        self.assertIn(field_in_message, dropped[0])
        self.assertIn("enrichment-only", dropped[0])

    def test_staple(self):
        self._assert_dropped("staple:any", "staple")
        self._assert_dropped("staple:cedh", "staple")

    def test_salt(self):
        self._assert_dropped("salt>=2", "salt")

    def test_combo(self):
        self._assert_dropped("combo:true", "combo")

    def test_buylist(self):
        self._assert_dropped("buylist:ck", "buylist")

    def test_cull(self):
        self._assert_dropped("cull:true", "cull")

    def test_deck(self):
        self._assert_dropped("deck:abc123", "deck")

    def test_wishlist(self):
        self._assert_dropped("wishlist:somebody", "wishlist")


class TestBooleanCombinations(unittest.TestCase):
    """Verify AND / OR / NOT render with correct Scryfall precedence."""

    def test_implicit_and(self):
        q, dropped = to_scryfall("t:creature c:rg")
        self.assertEqual(q, "t:creature c:rg")
        self.assertEqual(dropped, [])

    def test_explicit_or(self):
        q, dropped = to_scryfall("c:r or c:g")
        self.assertEqual(q, "c:r or c:g")
        self.assertEqual(dropped, [])

    def test_or_under_and_gets_parens(self):
        # AND of (something, OR-group) must parenthesize the OR-group
        # so Scryfall doesn't reassociate (Scryfall's `or` binds looser
        # than juxtaposition).
        q, _ = to_scryfall("t:creature (c:r or c:g)")
        self.assertEqual(q, "t:creature (c:r or c:g)")

    def test_not_single_term(self):
        q, _ = to_scryfall("NOT otag:ramp")
        self.assertEqual(q, "-otag:ramp")

    def test_not_group(self):
        q, _ = to_scryfall("NOT (c:r or c:g)")
        self.assertEqual(q, "-(c:r or c:g)")


class TestMixedDroppedAndKept(unittest.TestCase):
    """A query mixing translatable + non-translatable predicates yields
    a partial scryfall query and a list of what was dropped."""

    def test_one_kept_one_dropped(self):
        q, dropped = to_scryfall("usd>=1 staple:any")
        self.assertEqual(q, "usd>=1")
        self.assertEqual(len(dropped), 1)
        self.assertIn("staple:any", dropped[0])

    def test_all_dropped_yields_empty(self):
        q, dropped = to_scryfall("staple:any salt>=2")
        self.assertEqual(q, "")
        self.assertEqual(len(dropped), 2)

    def test_real_sort_preset(self):
        # Mirrors a real bin from the user's "money legends garbage ramp removal staples" preset.
        # Scryfall keeps the price + tag terms; the staple predicate drops.
        q, dropped = to_scryfall("usd<1 t:legend")
        self.assertEqual(q, "usd<1 t:legend")
        self.assertEqual(dropped, [])

        q2, dropped2 = to_scryfall("usd<1 staple:any")
        self.assertEqual(q2, "usd<1")
        self.assertIn("staple:any", dropped2[0])


class TestEdgeCases(unittest.TestCase):
    def test_empty_string(self):
        q, dropped = to_scryfall("")
        self.assertEqual(q, "")
        self.assertEqual(dropped, [])

    def test_whitespace_only(self):
        q, dropped = to_scryfall("    ")
        self.assertEqual(q, "")
        self.assertEqual(dropped, [])

    def test_parse_error_yields_dropped_message(self):
        q, dropped = to_scryfall("(((c:r")  # unbalanced parens
        self.assertEqual(q, "")
        self.assertEqual(len(dropped), 1)
        self.assertIn("parse", dropped[0].lower())


class TestUrlBuilder(unittest.TestCase):
    def test_basic_url(self):
        url, dropped = to_scryfall_url("usd>=1 t:creature")
        self.assertTrue(url.startswith("https://scryfall.com/search?q="))
        # urlencoded — space becomes %20 (quote default), `>=` becomes %3E%3D
        self.assertIn("usd%3E%3D1", url)
        self.assertIn("t%3Acreature", url)
        self.assertEqual(dropped, [])

    def test_url_empty_when_everything_dropped(self):
        url, dropped = to_scryfall_url("staple:any cull:true")
        self.assertEqual(url, "")
        self.assertEqual(len(dropped), 2)


if __name__ == "__main__":
    unittest.main()
