"""Unit tests for query_parser.py — tokenizer, parser, and evaluator."""

import sys
import os
import unittest

# Add parent dir so we can import query_parser
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from query_parser import (
    tokenize, parse_query, evaluate_query, collect_otag_terms,
    matches_query, QueryParseError,
    AndNode, OrNode, NotNode, FieldQuery,
)


# ---------------------------------------------------------------------------
# Sample card data for evaluator tests
# ---------------------------------------------------------------------------

LIGHTNING_BOLT = {
    "name": "Lightning Bolt",
    "colors": ["R"],
    "color_identity": ["R"],
    "cmc": 1.0,
    "type_line": "Instant",
    "set": "m11",
    "rarity": "common",
    "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    "keywords": [],
    "prices": {"usd": "1.50"},
    "power": None,
    "toughness": None,
    "legalities": {"modern": "legal", "standard": "not_legal", "commander": "legal"},
    "produced_mana": [],
    "set_type": "core",
    "oracle_id": "bolt-oracle-id",
    "layout": "normal",
    "promo": False,
    "reprint": True,
    "full_art": False,
    "foil": False,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}

TARMOGOYF = {
    "name": "Tarmogoyf",
    "colors": ["G"],
    "color_identity": ["G"],
    "cmc": 2.0,
    "type_line": "Creature — Lhurgoyf",
    "set": "fut",
    "rarity": "rare",
    "oracle_text": "Tarmogoyf's power is equal to the number of card types among cards in all graveyards and its toughness is that number plus 1.",
    "keywords": [],
    "prices": {"usd": "12.00"},
    "power": "*",
    "toughness": "1+*",
    "legalities": {"modern": "legal", "standard": "not_legal", "commander": "legal"},
    "produced_mana": [],
    "set_type": "expansion",
    "oracle_id": "goyf-oracle-id",
    "layout": "normal",
    "promo": False,
    "reprint": False,
    "full_art": False,
    "foil": False,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}

ATRAXA = {
    "name": "Atraxa, Praetors' Voice",
    "colors": ["W", "U", "B", "G"],
    "color_identity": ["W", "U", "B", "G"],
    "cmc": 4.0,
    "type_line": "Legendary Creature — Phyrexian Angel Horror",
    "set": "c16",
    "rarity": "mythic",
    "oracle_text": "Flying, vigilance, deathtouch, lifelink\nAt the beginning of your end step, proliferate.",
    "keywords": ["Flying", "Vigilance", "Deathtouch", "Lifelink"],
    "prices": {"usd": "25.00"},
    "power": "4",
    "toughness": "4",
    "legalities": {"modern": "not_legal", "standard": "not_legal", "commander": "legal"},
    "produced_mana": [],
    "set_type": "commander",
    "oracle_id": "atraxa-oracle-id",
    "layout": "normal",
    "promo": False,
    "reprint": False,
    "full_art": False,
    "foil": False,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}

SOL_RING = {
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
    "legalities": {"modern": "not_legal", "standard": "not_legal", "commander": "legal"},
    "produced_mana": ["C"],
    "set_type": "commander",
    "oracle_id": "sol-ring-oracle-id",
    "layout": "normal",
    "promo": False,
    "reprint": True,
    "full_art": False,
    "foil": False,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}

FOREST = {
    "name": "Forest",
    "colors": [],
    "color_identity": ["G"],
    "cmc": 0.0,
    "type_line": "Basic Land — Forest",
    "set": "m21",
    "rarity": "common",
    "oracle_text": "({T}: Add {G}.)",
    "keywords": [],
    "prices": {"usd": "0.05"},
    "power": None,
    "toughness": None,
    "legalities": {"modern": "legal", "standard": "legal", "commander": "legal"},
    "produced_mana": ["G"],
    "set_type": "core",
    "oracle_id": "forest-oracle-id",
    "layout": "normal",
    "promo": False,
    "reprint": True,
    "full_art": False,
    "foil": False,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}

GRIZZLY_BEARS = {
    "name": "Grizzly Bears",
    "colors": ["G"],
    "color_identity": ["G"],
    "cmc": 2.0,
    "type_line": "Creature — Bear",
    "set": "10e",
    "rarity": "common",
    "oracle_text": "",
    "keywords": [],
    "prices": {"usd": "0.10"},
    "power": "2",
    "toughness": "2",
    "legalities": {"modern": "legal", "standard": "not_legal", "commander": "legal"},
    "produced_mana": [],
    "set_type": "core",
    "oracle_id": "bears-oracle-id",
    "layout": "normal",
    "promo": False,
    "reprint": True,
    "full_art": False,
    "foil": False,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}


# ===========================================================================
# Tokenizer Tests
# ===========================================================================

class TestTokenizer(unittest.TestCase):

    def test_simple_field_query(self):
        tokens = tokenize("c:w")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].type, "FIELD_QUERY")
        self.assertEqual(tokens[0].value, ("color", ":", "w"))

    def test_field_alias_resolution(self):
        tokens = tokenize("mv>=3")
        self.assertEqual(tokens[0].value, ("mana_value", ">=", "3"))

    def test_multiple_field_queries(self):
        tokens = tokenize("c:w t:creature")
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0].value[0], "color")
        self.assertEqual(tokens[1].value[0], "type")

    def test_or_keyword(self):
        tokens = tokenize("c:w or c:u")
        self.assertEqual(len(tokens), 3)
        self.assertEqual(tokens[1].type, "OR")

    def test_not_prefix(self):
        tokens = tokenize("-t:land")
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0].type, "NOT")
        self.assertEqual(tokens[1].type, "FIELD_QUERY")

    def test_double_negation(self):
        """Regression: -t:land -t:creature should produce two NOT+FIELD pairs."""
        tokens = tokenize("-t:land -t:creature")
        self.assertEqual(len(tokens), 4)
        self.assertEqual(tokens[0].type, "NOT")
        self.assertEqual(tokens[1].value, ("type", ":", "land"))
        self.assertEqual(tokens[2].type, "NOT")
        self.assertEqual(tokens[3].value, ("type", ":", "creature"))

    def test_parentheses(self):
        tokens = tokenize("(c:w or c:u)")
        types = [t.type for t in tokens]
        self.assertEqual(types, ["LPAREN", "FIELD_QUERY", "OR", "FIELD_QUERY", "RPAREN"])

    def test_quoted_value(self):
        tokens = tokenize('o:"draw a card"')
        self.assertEqual(tokens[0].value, ("oracle", ":", "draw a card"))

    def test_bare_word_becomes_name_search(self):
        tokens = tokenize("bolt")
        self.assertEqual(tokens[0].value, ("name", ":", "bolt"))

    def test_comparison_operators(self):
        for op in ["<", ">", "<=", ">=", "!=", "="]:
            tokens = tokenize(f"cmc{op}3")
            self.assertEqual(tokens[0].value[1], op, f"Failed for operator {op}")

    def test_unknown_field_raises(self):
        with self.assertRaises(QueryParseError):
            tokenize("xyz:foo")

    def test_not_keyword_text(self):
        tokens = tokenize("not t:land")
        self.assertEqual(tokens[0].type, "NOT")

    def test_empty_query_returns_empty(self):
        tokens = tokenize("   ")
        self.assertEqual(tokens, [])


# ===========================================================================
# Parser Tests
# ===========================================================================

class TestParser(unittest.TestCase):

    def test_single_field(self):
        ast = parse_query("c:w")
        self.assertIsInstance(ast, FieldQuery)
        self.assertEqual(ast.field, "color")

    def test_implicit_and(self):
        ast = parse_query("c:w t:creature")
        self.assertIsInstance(ast, AndNode)
        self.assertEqual(len(ast.children), 2)

    def test_explicit_or(self):
        ast = parse_query("c:w or c:u")
        self.assertIsInstance(ast, OrNode)
        self.assertEqual(len(ast.children), 2)

    def test_not(self):
        ast = parse_query("-t:land")
        self.assertIsInstance(ast, NotNode)
        self.assertIsInstance(ast.child, FieldQuery)

    def test_parenthesized_or(self):
        ast = parse_query("(c:w or c:u) t:creature")
        self.assertIsInstance(ast, AndNode)
        self.assertIsInstance(ast.children[0], OrNode)

    def test_complex_nested(self):
        ast = parse_query("(c:w or c:u) -t:land cmc<=3")
        self.assertIsInstance(ast, AndNode)
        self.assertEqual(len(ast.children), 3)
        self.assertIsInstance(ast.children[1], NotNode)

    def test_empty_query_raises(self):
        with self.assertRaises(QueryParseError):
            parse_query("")

    def test_double_negation_parsed(self):
        ast = parse_query("-t:land -t:creature")
        self.assertIsInstance(ast, AndNode)
        self.assertIsInstance(ast.children[0], NotNode)
        self.assertIsInstance(ast.children[1], NotNode)

    def test_or_precedence(self):
        """a OR b c  should be  OR(a, AND(b, c))"""
        ast = parse_query("c:w or c:u t:creature")
        self.assertIsInstance(ast, OrNode)
        self.assertIsInstance(ast.children[1], AndNode)

    def test_multiple_or(self):
        ast = parse_query("c:w or c:u or c:b")
        self.assertIsInstance(ast, OrNode)
        self.assertEqual(len(ast.children), 3)


# ===========================================================================
# Evaluator Tests — Color
# ===========================================================================

class TestEvalColor(unittest.TestCase):

    def test_has_color(self):
        self.assertTrue(matches_query("c:r", LIGHTNING_BOLT))
        self.assertFalse(matches_query("c:w", LIGHTNING_BOLT))

    def test_multicolor(self):
        self.assertTrue(matches_query("c:m", ATRAXA))
        self.assertFalse(matches_query("c:m", LIGHTNING_BOLT))

    def test_colorless(self):
        self.assertTrue(matches_query("c:c", SOL_RING))
        self.assertFalse(matches_query("c:c", LIGHTNING_BOLT))

    def test_exact_color(self):
        self.assertTrue(matches_query("c=r", LIGHTNING_BOLT))
        self.assertFalse(matches_query("c=rg", LIGHTNING_BOLT))

    def test_color_count_comparison(self):
        self.assertTrue(matches_query("c>=2", ATRAXA))
        self.assertFalse(matches_query("c>=2", LIGHTNING_BOLT))

    def test_color_identity(self):
        self.assertTrue(matches_query("id:g", FOREST))
        self.assertFalse(matches_query("id:r", FOREST))


# ===========================================================================
# Evaluator Tests — Type
# ===========================================================================

class TestEvalType(unittest.TestCase):

    def test_type_match(self):
        self.assertTrue(matches_query("t:instant", LIGHTNING_BOLT))
        self.assertFalse(matches_query("t:creature", LIGHTNING_BOLT))

    def test_type_creature(self):
        self.assertTrue(matches_query("t:creature", TARMOGOYF))
        self.assertTrue(matches_query("t:creature", ATRAXA))

    def test_type_land(self):
        self.assertTrue(matches_query("t:land", FOREST))
        self.assertFalse(matches_query("t:land", LIGHTNING_BOLT))

    def test_type_legendary(self):
        self.assertTrue(matches_query("t:legendary", ATRAXA))
        self.assertFalse(matches_query("t:legendary", TARMOGOYF))

    def test_type_artifact(self):
        self.assertTrue(matches_query("t:artifact", SOL_RING))


# ===========================================================================
# Evaluator Tests — Mana Value
# ===========================================================================

class TestEvalManaValue(unittest.TestCase):

    def test_exact_cmc(self):
        self.assertTrue(matches_query("cmc=1", LIGHTNING_BOLT))
        self.assertFalse(matches_query("cmc=2", LIGHTNING_BOLT))

    def test_cmc_less(self):
        self.assertTrue(matches_query("cmc<3", LIGHTNING_BOLT))
        self.assertFalse(matches_query("cmc<1", LIGHTNING_BOLT))

    def test_cmc_greater(self):
        self.assertTrue(matches_query("cmc>=4", ATRAXA))
        self.assertFalse(matches_query("cmc>4", ATRAXA))

    def test_cmc_lte(self):
        self.assertTrue(matches_query("mv<=2", TARMOGOYF))


# ===========================================================================
# Evaluator Tests — Set, Rarity, Price
# ===========================================================================

class TestEvalSetRarityPrice(unittest.TestCase):

    def test_set_match(self):
        self.assertTrue(matches_query("set:m11", LIGHTNING_BOLT))
        self.assertFalse(matches_query("set:m21", LIGHTNING_BOLT))

    def test_set_alias(self):
        self.assertTrue(matches_query("e:fut", TARMOGOYF))

    def test_rarity_exact(self):
        self.assertTrue(matches_query("r:mythic", ATRAXA))
        self.assertFalse(matches_query("r:rare", ATRAXA))

    def test_rarity_comparison(self):
        self.assertTrue(matches_query("r>=rare", ATRAXA))
        self.assertTrue(matches_query("r>=rare", TARMOGOYF))
        self.assertFalse(matches_query("r>=rare", LIGHTNING_BOLT))

    def test_price_comparison(self):
        self.assertTrue(matches_query("usd>=10", TARMOGOYF))
        self.assertTrue(matches_query("usd<2", LIGHTNING_BOLT))
        self.assertFalse(matches_query("usd>=10", LIGHTNING_BOLT))

    def test_price_no_price(self):
        card_no_price = {**LIGHTNING_BOLT, "prices": {}}
        self.assertFalse(matches_query("usd>=0", card_no_price))


# ===========================================================================
# Evaluator Tests — Power, Toughness
# ===========================================================================

class TestEvalPowerToughness(unittest.TestCase):

    def test_power_match(self):
        self.assertTrue(matches_query("pow>=2", GRIZZLY_BEARS))
        self.assertTrue(matches_query("pow=4", ATRAXA))

    def test_power_star_returns_false(self):
        self.assertFalse(matches_query("pow>=0", TARMOGOYF))

    def test_toughness_match(self):
        self.assertTrue(matches_query("tou>=4", ATRAXA))

    def test_power_none(self):
        self.assertFalse(matches_query("pow>=0", LIGHTNING_BOLT))


# ===========================================================================
# Evaluator Tests — Oracle Text, Name, Keywords
# ===========================================================================

class TestEvalTextFields(unittest.TestCase):

    def test_oracle_contains(self):
        self.assertTrue(matches_query("o:damage", LIGHTNING_BOLT))
        self.assertFalse(matches_query("o:draw", LIGHTNING_BOLT))

    def test_oracle_quoted(self):
        self.assertTrue(matches_query('o:"3 damage"', LIGHTNING_BOLT))

    def test_name_search(self):
        self.assertTrue(matches_query("name:bolt", LIGHTNING_BOLT))
        self.assertTrue(matches_query("name:lightning", LIGHTNING_BOLT))
        self.assertFalse(matches_query("name:shock", LIGHTNING_BOLT))

    def test_bare_word_is_name(self):
        self.assertTrue(matches_query("bolt", LIGHTNING_BOLT))

    def test_keyword_match(self):
        self.assertTrue(matches_query("kw:flying", ATRAXA))
        self.assertTrue(matches_query("keyword:deathtouch", ATRAXA))
        self.assertFalse(matches_query("kw:flying", LIGHTNING_BOLT))


# ===========================================================================
# Evaluator Tests — Legality, Produces, Set Type
# ===========================================================================

class TestEvalMiscFields(unittest.TestCase):

    def test_legal_format(self):
        self.assertTrue(matches_query("legal:commander", LIGHTNING_BOLT))
        self.assertTrue(matches_query("legal:modern", LIGHTNING_BOLT))
        self.assertFalse(matches_query("legal:standard", LIGHTNING_BOLT))

    def test_produces(self):
        self.assertTrue(matches_query("produces:G", FOREST))
        self.assertTrue(matches_query("produces:C", SOL_RING))
        self.assertFalse(matches_query("produces:R", FOREST))

    def test_set_type(self):
        self.assertTrue(matches_query("st:core", LIGHTNING_BOLT))
        self.assertTrue(matches_query("st:expansion", TARMOGOYF))
        self.assertFalse(matches_query("st:core", TARMOGOYF))


# ===========================================================================
# Evaluator Tests — is: predicates
# ===========================================================================

class TestEvalIsPredicate(unittest.TestCase):

    def test_is_land(self):
        self.assertTrue(matches_query("is:land", FOREST))
        self.assertFalse(matches_query("is:land", LIGHTNING_BOLT))

    def test_is_creature(self):
        self.assertTrue(matches_query("is:creature", TARMOGOYF))

    def test_is_spell(self):
        self.assertTrue(matches_query("is:spell", LIGHTNING_BOLT))
        self.assertFalse(matches_query("is:spell", FOREST))

    def test_is_legendary(self):
        self.assertTrue(matches_query("is:legendary", ATRAXA))
        self.assertFalse(matches_query("is:legendary", LIGHTNING_BOLT))

    def test_is_multicolor(self):
        self.assertTrue(matches_query("is:multicolor", ATRAXA))
        self.assertFalse(matches_query("is:multicolor", LIGHTNING_BOLT))

    def test_is_colorless(self):
        self.assertTrue(matches_query("is:colorless", SOL_RING))
        self.assertFalse(matches_query("is:colorless", LIGHTNING_BOLT))

    def test_is_mono(self):
        self.assertTrue(matches_query("is:mono", LIGHTNING_BOLT))
        self.assertFalse(matches_query("is:mono", ATRAXA))

    def test_is_reprint(self):
        self.assertTrue(matches_query("is:reprint", LIGHTNING_BOLT))
        self.assertFalse(matches_query("is:reprint", TARMOGOYF))

    def test_is_basic(self):
        self.assertTrue(matches_query("is:basic", FOREST))
        self.assertFalse(matches_query("is:basic", LIGHTNING_BOLT))

    def test_is_historic(self):
        # Legendary or artifact or saga
        self.assertTrue(matches_query("is:historic", ATRAXA))
        self.assertTrue(matches_query("is:historic", SOL_RING))
        self.assertFalse(matches_query("is:historic", LIGHTNING_BOLT))


# ===========================================================================
# Evaluator Tests — otag
# ===========================================================================

class TestEvalOtag(unittest.TestCase):

    def test_otag_match(self):
        cache = {"removal": {"bolt-oracle-id", "other-id"}}
        self.assertTrue(matches_query("otag:removal", LIGHTNING_BOLT, otag_cache=cache))
        self.assertFalse(matches_query("otag:removal", TARMOGOYF, otag_cache=cache))

    def test_otag_no_cache(self):
        self.assertFalse(matches_query("otag:removal", LIGHTNING_BOLT))

    def test_otag_unknown_tag(self):
        cache = {"ramp": {"sol-ring-oracle-id"}}
        self.assertFalse(matches_query("otag:removal", LIGHTNING_BOLT, otag_cache=cache))


# ===========================================================================
# Evaluator Tests — Compound queries
# ===========================================================================

class TestEvalCompound(unittest.TestCase):

    def test_and(self):
        self.assertTrue(matches_query("c:r t:instant", LIGHTNING_BOLT))
        self.assertFalse(matches_query("c:w t:instant", LIGHTNING_BOLT))

    def test_or(self):
        self.assertTrue(matches_query("c:r or c:g", LIGHTNING_BOLT))
        self.assertTrue(matches_query("c:r or c:g", TARMOGOYF))
        self.assertFalse(matches_query("c:w or c:u", LIGHTNING_BOLT))

    def test_not(self):
        self.assertTrue(matches_query("-t:creature", LIGHTNING_BOLT))
        self.assertFalse(matches_query("-t:instant", LIGHTNING_BOLT))

    def test_complex_compound(self):
        # Red OR green, NOT a land, cmc <= 2
        query = "(c:r or c:g) -t:land cmc<=2"
        self.assertTrue(matches_query(query, LIGHTNING_BOLT))
        self.assertTrue(matches_query(query, TARMOGOYF))
        self.assertFalse(matches_query(query, ATRAXA))  # cmc 4
        self.assertFalse(matches_query(query, FOREST))   # land

    def test_double_negation_query(self):
        """Regression: -t:land -t:creature should exclude both."""
        query = "-t:land -t:creature"
        self.assertTrue(matches_query(query, LIGHTNING_BOLT))   # instant
        self.assertTrue(matches_query(query, SOL_RING))          # artifact
        self.assertFalse(matches_query(query, TARMOGOYF))        # creature
        self.assertFalse(matches_query(query, FOREST))           # land


# ===========================================================================
# collect_otag_terms
# ===========================================================================

class TestCollectOtagTerms(unittest.TestCase):

    def test_single_otag(self):
        ast = parse_query("otag:removal")
        self.assertEqual(collect_otag_terms(ast), {"removal"})

    def test_multiple_otags(self):
        ast = parse_query("otag:removal or otag:ramp")
        self.assertEqual(collect_otag_terms(ast), {"removal", "ramp"})

    def test_no_otags(self):
        ast = parse_query("c:w t:creature")
        self.assertEqual(collect_otag_terms(ast), set())

    def test_otag_in_not(self):
        ast = parse_query("-otag:removal")
        self.assertEqual(collect_otag_terms(ast), {"removal"})

    def test_nested_otag(self):
        ast = parse_query("(otag:ramp or otag:removal) -t:land")
        self.assertEqual(collect_otag_terms(ast), {"ramp", "removal"})


if __name__ == "__main__":
    unittest.main()
