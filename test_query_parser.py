# test_query_parser.py
# Unit tests for the Scryfall-like query parser and evaluator.

import unittest
from query_parser import (
    tokenize, parse_query, evaluate_query, collect_otag_terms,
    matches_query, QueryParseError,
    AndNode, OrNode, NotNode, FieldQuery,
    Token
)


# ---------------------------------------------------------------------------
# Sample card data for evaluation tests
# ---------------------------------------------------------------------------

LIGHTNING_BOLT = {
    "name": "Lightning Bolt",
    "set": "m11",
    "set_type": "core",
    "colors": ["R"],
    "color_identity": ["R"],
    "cmc": 1.0,
    "type_line": "Instant",
    "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    "rarity": "common",
    "prices": {"usd": "1.50"},
    "keywords": [],
    "legalities": {"modern": "legal", "standard": "not_legal", "commander": "legal"},
    "produced_mana": [],
    "oracle_id": "bolt-oracle-001",
    "layout": "normal",
    "promo": False,
    "reprint": True,
    "full_art": False,
    "foil": False,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}

BIRDS_OF_PARADISE = {
    "name": "Birds of Paradise",
    "set": "m12",
    "set_type": "core",
    "colors": ["G"],
    "color_identity": ["G"],
    "cmc": 1.0,
    "type_line": "Creature — Bird",
    "oracle_text": "{T}: Add one mana of any color.",
    "rarity": "rare",
    "power": "0",
    "toughness": "1",
    "prices": {"usd": "8.00"},
    "keywords": ["Flying"],
    "legalities": {"modern": "legal", "standard": "not_legal", "commander": "legal"},
    "produced_mana": ["W", "U", "B", "R", "G"],
    "oracle_id": "birds-oracle-002",
    "layout": "normal",
    "promo": False,
    "reprint": True,
    "full_art": False,
    "foil": True,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}

WRATH_OF_GOD = {
    "name": "Wrath of God",
    "set": "2xm",
    "set_type": "masters",
    "colors": ["W"],
    "color_identity": ["W"],
    "cmc": 4.0,
    "type_line": "Sorcery",
    "oracle_text": "Destroy all creatures. They can't be regenerated.",
    "rarity": "rare",
    "prices": {"usd": "5.25"},
    "keywords": [],
    "legalities": {"modern": "legal", "standard": "not_legal", "commander": "legal"},
    "produced_mana": [],
    "oracle_id": "wrath-oracle-003",
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
    "set": "m21",
    "set_type": "core",
    "colors": [],
    "color_identity": ["G"],
    "cmc": 0.0,
    "type_line": "Basic Land — Forest",
    "oracle_text": "({T}: Add {G}.)",
    "rarity": "common",
    "prices": {"usd": "0.10"},
    "keywords": [],
    "legalities": {"modern": "legal", "standard": "legal", "commander": "legal"},
    "produced_mana": ["G"],
    "oracle_id": "forest-oracle-004",
    "layout": "normal",
    "promo": False,
    "reprint": True,
    "full_art": False,
    "foil": False,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}

NICOL_BOLAS = {
    "name": "Nicol Bolas, the Ravager",
    "set": "m19",
    "set_type": "expansion",
    "colors": ["U", "B", "R"],
    "color_identity": ["U", "B", "R"],
    "cmc": 4.0,
    "type_line": "Legendary Creature — Elder Dragon",
    "oracle_text": "Flying\nWhen Nicol Bolas, the Ravager enters the battlefield, "
                   "each opponent discards a card.",
    "rarity": "mythic",
    "power": "4",
    "toughness": "4",
    "prices": {"usd": "15.00"},
    "keywords": ["Flying"],
    "legalities": {"modern": "legal", "standard": "not_legal", "commander": "legal"},
    "produced_mana": [],
    "oracle_id": "bolas-oracle-005",
    "layout": "transform",
    "promo": False,
    "reprint": False,
    "full_art": False,
    "foil": True,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}

SOL_RING = {
    "name": "Sol Ring",
    "set": "c21",
    "set_type": "commander",
    "colors": [],
    "color_identity": [],
    "cmc": 1.0,
    "type_line": "Artifact",
    "oracle_text": "{T}: Add {C}{C}.",
    "rarity": "uncommon",
    "prices": {"usd": "2.50"},
    "keywords": [],
    "legalities": {"modern": "not_legal", "standard": "not_legal",
                   "commander": "legal", "vintage": "restricted"},
    "produced_mana": ["C"],
    "oracle_id": "solring-oracle-006",
    "layout": "normal",
    "promo": False,
    "reprint": True,
    "full_art": False,
    "foil": False,
    "nonfoil": True,
    "digital": False,
    "reserved": False,
}


# ---------------------------------------------------------------------------
# Tokenizer Tests
# ---------------------------------------------------------------------------

class TestTokenizer(unittest.TestCase):

    def test_simple_field_query(self):
        tokens = tokenize("c:w")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].type, 'FIELD_QUERY')
        self.assertEqual(tokens[0].value, ('color', ':', 'w'))

    def test_field_with_comparison(self):
        tokens = tokenize("usd>=10")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].value, ('price', '>=', '10'))

    def test_less_than_equal(self):
        tokens = tokenize("cmc<=3")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].value, ('mana_value', '<=', '3'))

    def test_not_equal(self):
        tokens = tokenize("r!=common")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].value, ('rarity', '!=', 'common'))

    def test_implicit_and(self):
        tokens = tokenize("c:w t:creature")
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0].value[0], 'color')
        self.assertEqual(tokens[1].value[0], 'type')

    def test_explicit_or(self):
        tokens = tokenize("c:w or c:u")
        self.assertEqual(len(tokens), 3)
        self.assertEqual(tokens[0].type, 'FIELD_QUERY')
        self.assertEqual(tokens[1].type, 'OR')
        self.assertEqual(tokens[2].type, 'FIELD_QUERY')

    def test_negation(self):
        tokens = tokenize("-t:land")
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0].type, 'NOT')
        self.assertEqual(tokens[1].type, 'FIELD_QUERY')
        self.assertEqual(tokens[1].value, ('type', ':', 'land'))

    def test_double_negation(self):
        """Two separate negated queries: -t:land -t:creature"""
        tokens = tokenize("-t:land -t:creature")
        self.assertEqual(len(tokens), 4)
        self.assertEqual(tokens[0].type, 'NOT')
        self.assertEqual(tokens[1].value, ('type', ':', 'land'))
        self.assertEqual(tokens[2].type, 'NOT')
        self.assertEqual(tokens[3].value, ('type', ':', 'creature'))

    def test_parentheses(self):
        tokens = tokenize("(c:w or c:u) t:creature")
        types = [t.type for t in tokens]
        self.assertEqual(types, ['LPAREN', 'FIELD_QUERY', 'OR',
                                  'FIELD_QUERY', 'RPAREN', 'FIELD_QUERY'])

    def test_quoted_value(self):
        tokens = tokenize('o:"draw a card"')
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].value, ('oracle', ':', 'draw a card'))

    def test_bare_word_becomes_name_search(self):
        tokens = tokenize("bolt")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].value, ('name', ':', 'bolt'))

    def test_not_keyword(self):
        tokens = tokenize("not t:land")
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0].type, 'NOT')
        self.assertEqual(tokens[1].type, 'FIELD_QUERY')

    def test_unknown_field_raises(self):
        with self.assertRaises(QueryParseError):
            tokenize("zzz:value")

    def test_field_aliases(self):
        """Multiple aliases resolve to the same canonical field."""
        tests = [
            ("color:w", "color"), ("c:w", "color"),
            ("ci:w", "color_identity"), ("id:w", "color_identity"),
            ("type:creature", "type"), ("t:creature", "type"),
            ("mv:3", "mana_value"), ("cmc:3", "mana_value"),
            ("rarity:rare", "rarity"), ("r:rare", "rarity"),
            ("usd>=5", "price"), ("price>=5", "price"),
            ("kw:flying", "keyword"), ("keyword:flying", "keyword"),
            ("legal:modern", "legal"), ("format:modern", "legal"),
            ("st:core", "set_type"), ("settype:core", "set_type"),
            ("otag:ramp", "otag"), ("oracletag:ramp", "otag"),
            ("function:removal", "otag"),
        ]
        for query, expected_field in tests:
            tokens = tokenize(query)
            self.assertEqual(tokens[0].value[0], expected_field,
                             f"Failed for query '{query}'")


# ---------------------------------------------------------------------------
# Parser Tests
# ---------------------------------------------------------------------------

class TestParser(unittest.TestCase):

    def test_single_query(self):
        ast = parse_query("c:w")
        self.assertIsInstance(ast, FieldQuery)
        self.assertEqual(ast.field, 'color')
        self.assertEqual(ast.value, 'w')

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
        self.assertEqual(ast.child.field, 'type')

    def test_grouped_or_with_and(self):
        """(c:w or c:u) t:creature"""
        ast = parse_query("(c:w or c:u) t:creature")
        self.assertIsInstance(ast, AndNode)
        self.assertEqual(len(ast.children), 2)
        self.assertIsInstance(ast.children[0], OrNode)
        self.assertIsInstance(ast.children[1], FieldQuery)

    def test_complex_nested(self):
        """(c:r or c:g) -t:land cmc<=3"""
        ast = parse_query("(c:r or c:g) -t:land cmc<=3")
        self.assertIsInstance(ast, AndNode)
        self.assertEqual(len(ast.children), 3)
        self.assertIsInstance(ast.children[0], OrNode)
        self.assertIsInstance(ast.children[1], NotNode)
        self.assertIsInstance(ast.children[2], FieldQuery)

    def test_double_negation_parse(self):
        """-t:land -t:creature"""
        ast = parse_query("-t:land -t:creature")
        self.assertIsInstance(ast, AndNode)
        self.assertEqual(len(ast.children), 2)
        self.assertIsInstance(ast.children[0], NotNode)
        self.assertIsInstance(ast.children[1], NotNode)

    def test_or_precedence(self):
        """a:x b:y or c:z => (a:x AND b:y) OR (c:z)"""
        # Actually: or_expr = and_expr ('or' and_expr)*
        # so "name:x name:y or name:z" = OR(AND(name:x, name:y), name:z)
        ast = parse_query("t:creature c:w or t:land")
        self.assertIsInstance(ast, OrNode)
        self.assertEqual(len(ast.children), 2)
        self.assertIsInstance(ast.children[0], AndNode)
        self.assertIsInstance(ast.children[1], FieldQuery)

    def test_empty_query_raises(self):
        with self.assertRaises(QueryParseError):
            parse_query("")

    def test_unmatched_paren_raises(self):
        with self.assertRaises(QueryParseError):
            parse_query("(c:w")


# ---------------------------------------------------------------------------
# Evaluator Tests — Color
# ---------------------------------------------------------------------------

class TestEvalColor(unittest.TestCase):

    def test_color_contains(self):
        self.assertTrue(matches_query("c:r", LIGHTNING_BOLT))
        self.assertFalse(matches_query("c:w", LIGHTNING_BOLT))

    def test_color_exact(self):
        self.assertTrue(matches_query("c=r", LIGHTNING_BOLT))
        self.assertFalse(matches_query("c=w", LIGHTNING_BOLT))

    def test_multicolor_contains(self):
        self.assertTrue(matches_query("c:u", NICOL_BOLAS))
        self.assertTrue(matches_query("c:b", NICOL_BOLAS))
        self.assertTrue(matches_query("c:r", NICOL_BOLAS))
        self.assertFalse(matches_query("c:w", NICOL_BOLAS))

    def test_multicolor_exact(self):
        self.assertTrue(matches_query("c=ubr", NICOL_BOLAS))
        self.assertFalse(matches_query("c=ub", NICOL_BOLAS))

    def test_color_multicolor_special(self):
        self.assertTrue(matches_query("c:m", NICOL_BOLAS))
        self.assertFalse(matches_query("c:m", LIGHTNING_BOLT))

    def test_color_colorless(self):
        self.assertTrue(matches_query("c:c", SOL_RING))
        self.assertTrue(matches_query("c:c", FOREST))
        self.assertFalse(matches_query("c:c", LIGHTNING_BOLT))

    def test_color_identity(self):
        self.assertTrue(matches_query("ci:g", FOREST))
        self.assertFalse(matches_query("ci:r", FOREST))


# ---------------------------------------------------------------------------
# Evaluator Tests — Type
# ---------------------------------------------------------------------------

class TestEvalType(unittest.TestCase):

    def test_type_match(self):
        self.assertTrue(matches_query("t:instant", LIGHTNING_BOLT))
        self.assertFalse(matches_query("t:creature", LIGHTNING_BOLT))

    def test_type_creature(self):
        self.assertTrue(matches_query("t:creature", BIRDS_OF_PARADISE))
        self.assertTrue(matches_query("t:creature", NICOL_BOLAS))

    def test_type_land(self):
        self.assertTrue(matches_query("t:land", FOREST))
        self.assertFalse(matches_query("t:land", LIGHTNING_BOLT))

    def test_type_subtype(self):
        self.assertTrue(matches_query("t:bird", BIRDS_OF_PARADISE))
        self.assertTrue(matches_query("t:dragon", NICOL_BOLAS))

    def test_type_artifact(self):
        self.assertTrue(matches_query("t:artifact", SOL_RING))
        self.assertFalse(matches_query("t:artifact", LIGHTNING_BOLT))


# ---------------------------------------------------------------------------
# Evaluator Tests — Mana Value
# ---------------------------------------------------------------------------

class TestEvalManaValue(unittest.TestCase):

    def test_equal(self):
        self.assertTrue(matches_query("cmc:1", LIGHTNING_BOLT))
        self.assertFalse(matches_query("cmc:2", LIGHTNING_BOLT))

    def test_greater_equal(self):
        self.assertTrue(matches_query("cmc>=4", WRATH_OF_GOD))
        self.assertTrue(matches_query("cmc>=4", NICOL_BOLAS))
        self.assertFalse(matches_query("cmc>=4", LIGHTNING_BOLT))

    def test_less_than(self):
        self.assertTrue(matches_query("cmc<2", LIGHTNING_BOLT))
        self.assertFalse(matches_query("cmc<1", LIGHTNING_BOLT))

    def test_less_equal(self):
        self.assertTrue(matches_query("cmc<=1", LIGHTNING_BOLT))
        self.assertTrue(matches_query("cmc<=4", WRATH_OF_GOD))


# ---------------------------------------------------------------------------
# Evaluator Tests — Set
# ---------------------------------------------------------------------------

class TestEvalSet(unittest.TestCase):

    def test_set_match(self):
        self.assertTrue(matches_query("set:m11", LIGHTNING_BOLT))
        self.assertFalse(matches_query("set:m12", LIGHTNING_BOLT))

    def test_set_not_equal(self):
        self.assertTrue(matches_query("set!=m11", BIRDS_OF_PARADISE))
        self.assertFalse(matches_query("set!=m11", LIGHTNING_BOLT))


# ---------------------------------------------------------------------------
# Evaluator Tests — Rarity
# ---------------------------------------------------------------------------

class TestEvalRarity(unittest.TestCase):

    def test_rarity_exact(self):
        self.assertTrue(matches_query("r:common", LIGHTNING_BOLT))
        self.assertTrue(matches_query("r:rare", WRATH_OF_GOD))
        self.assertTrue(matches_query("r:mythic", NICOL_BOLAS))

    def test_rarity_comparison(self):
        self.assertTrue(matches_query("r>=rare", NICOL_BOLAS))
        self.assertTrue(matches_query("r>=rare", WRATH_OF_GOD))
        self.assertFalse(matches_query("r>=rare", LIGHTNING_BOLT))

    def test_rarity_less_than(self):
        self.assertTrue(matches_query("r<rare", LIGHTNING_BOLT))
        self.assertFalse(matches_query("r<rare", WRATH_OF_GOD))


# ---------------------------------------------------------------------------
# Evaluator Tests — Price
# ---------------------------------------------------------------------------

class TestEvalPrice(unittest.TestCase):

    def test_price_greater(self):
        self.assertTrue(matches_query("usd>=10", NICOL_BOLAS))
        self.assertFalse(matches_query("usd>=10", LIGHTNING_BOLT))

    def test_price_less(self):
        self.assertTrue(matches_query("usd<1", FOREST))
        self.assertFalse(matches_query("usd<1", LIGHTNING_BOLT))

    def test_price_range(self):
        self.assertTrue(matches_query("usd>=5 usd<10", BIRDS_OF_PARADISE))
        self.assertFalse(matches_query("usd>=5 usd<10", NICOL_BOLAS))


# ---------------------------------------------------------------------------
# Evaluator Tests — Power/Toughness
# ---------------------------------------------------------------------------

class TestEvalPowerToughness(unittest.TestCase):

    def test_power(self):
        self.assertTrue(matches_query("pow:0", BIRDS_OF_PARADISE))
        self.assertTrue(matches_query("pow>=4", NICOL_BOLAS))
        self.assertFalse(matches_query("pow>=1", BIRDS_OF_PARADISE))

    def test_toughness(self):
        self.assertTrue(matches_query("tou:1", BIRDS_OF_PARADISE))
        self.assertTrue(matches_query("tou>=4", NICOL_BOLAS))

    def test_no_power(self):
        """Cards without power (instants, etc.) should not match."""
        self.assertFalse(matches_query("pow:0", LIGHTNING_BOLT))


# ---------------------------------------------------------------------------
# Evaluator Tests — Oracle Text
# ---------------------------------------------------------------------------

class TestEvalOracle(unittest.TestCase):

    def test_oracle_match(self):
        self.assertTrue(matches_query("o:damage", LIGHTNING_BOLT))
        self.assertTrue(matches_query("o:destroy", WRATH_OF_GOD))

    def test_oracle_quoted(self):
        self.assertTrue(matches_query('o:"any target"', LIGHTNING_BOLT))
        self.assertFalse(matches_query('o:"any target"', WRATH_OF_GOD))

    def test_oracle_no_match(self):
        self.assertFalse(matches_query("o:flying", LIGHTNING_BOLT))


# ---------------------------------------------------------------------------
# Evaluator Tests — Name
# ---------------------------------------------------------------------------

class TestEvalName(unittest.TestCase):

    def test_name_substring(self):
        self.assertTrue(matches_query("name:bolt", LIGHTNING_BOLT))
        self.assertTrue(matches_query("name:birds", BIRDS_OF_PARADISE))

    def test_bare_word_name_search(self):
        """Bare words are treated as name searches."""
        self.assertTrue(matches_query("bolt", LIGHTNING_BOLT))
        self.assertFalse(matches_query("bolt", BIRDS_OF_PARADISE))


# ---------------------------------------------------------------------------
# Evaluator Tests — Keywords
# ---------------------------------------------------------------------------

class TestEvalKeyword(unittest.TestCase):

    def test_keyword_match(self):
        self.assertTrue(matches_query("kw:flying", BIRDS_OF_PARADISE))
        self.assertTrue(matches_query("kw:flying", NICOL_BOLAS))

    def test_keyword_no_match(self):
        self.assertFalse(matches_query("kw:flying", LIGHTNING_BOLT))
        self.assertFalse(matches_query("kw:trample", BIRDS_OF_PARADISE))


# ---------------------------------------------------------------------------
# Evaluator Tests — Format Legality
# ---------------------------------------------------------------------------

class TestEvalLegal(unittest.TestCase):

    def test_legal(self):
        self.assertTrue(matches_query("legal:modern", LIGHTNING_BOLT))
        self.assertTrue(matches_query("legal:commander", SOL_RING))

    def test_not_legal(self):
        self.assertFalse(matches_query("legal:modern", SOL_RING))
        self.assertFalse(matches_query("legal:standard", LIGHTNING_BOLT))

    def test_restricted_counts_as_legal(self):
        self.assertTrue(matches_query("legal:vintage", SOL_RING))


# ---------------------------------------------------------------------------
# Evaluator Tests — Produces
# ---------------------------------------------------------------------------

class TestEvalProduces(unittest.TestCase):

    def test_produces_any_color(self):
        self.assertTrue(matches_query("produces:W", BIRDS_OF_PARADISE))
        self.assertTrue(matches_query("produces:G", BIRDS_OF_PARADISE))

    def test_produces_colorless(self):
        self.assertTrue(matches_query("produces:C", SOL_RING))

    def test_produces_no_match(self):
        self.assertFalse(matches_query("produces:W", LIGHTNING_BOLT))


# ---------------------------------------------------------------------------
# Evaluator Tests — Set Type
# ---------------------------------------------------------------------------

class TestEvalSetType(unittest.TestCase):

    def test_set_type(self):
        self.assertTrue(matches_query("st:core", LIGHTNING_BOLT))
        self.assertTrue(matches_query("st:masters", WRATH_OF_GOD))
        self.assertTrue(matches_query("st:commander", SOL_RING))

    def test_set_type_not_equal(self):
        self.assertTrue(matches_query("st!=core", WRATH_OF_GOD))
        self.assertFalse(matches_query("st!=core", LIGHTNING_BOLT))


# ---------------------------------------------------------------------------
# Evaluator Tests — is: Predicates
# ---------------------------------------------------------------------------

class TestEvalIs(unittest.TestCase):

    def test_is_land(self):
        self.assertTrue(matches_query("is:land", FOREST))
        self.assertFalse(matches_query("is:land", LIGHTNING_BOLT))

    def test_is_creature(self):
        self.assertTrue(matches_query("is:creature", BIRDS_OF_PARADISE))
        self.assertFalse(matches_query("is:creature", LIGHTNING_BOLT))

    def test_is_spell(self):
        """Spells = not lands."""
        self.assertTrue(matches_query("is:spell", LIGHTNING_BOLT))
        self.assertTrue(matches_query("is:spell", BIRDS_OF_PARADISE))
        self.assertFalse(matches_query("is:spell", FOREST))

    def test_is_permanent(self):
        self.assertTrue(matches_query("is:permanent", BIRDS_OF_PARADISE))
        self.assertTrue(matches_query("is:permanent", FOREST))
        self.assertFalse(matches_query("is:permanent", LIGHTNING_BOLT))

    def test_is_multicolor(self):
        self.assertTrue(matches_query("is:multicolor", NICOL_BOLAS))
        self.assertFalse(matches_query("is:multicolor", LIGHTNING_BOLT))

    def test_is_colorless(self):
        self.assertTrue(matches_query("is:colorless", SOL_RING))
        self.assertFalse(matches_query("is:colorless", LIGHTNING_BOLT))

    def test_is_mono(self):
        self.assertTrue(matches_query("is:mono", LIGHTNING_BOLT))
        self.assertFalse(matches_query("is:mono", NICOL_BOLAS))
        self.assertFalse(matches_query("is:mono", SOL_RING))

    def test_is_basic(self):
        self.assertTrue(matches_query("is:basic", FOREST))
        self.assertFalse(matches_query("is:basic", BIRDS_OF_PARADISE))

    def test_is_legendary(self):
        self.assertTrue(matches_query("is:legendary", NICOL_BOLAS))
        self.assertFalse(matches_query("is:legendary", LIGHTNING_BOLT))

    def test_is_reprint(self):
        self.assertTrue(matches_query("is:reprint", LIGHTNING_BOLT))
        self.assertFalse(matches_query("is:reprint", NICOL_BOLAS))

    def test_is_foil(self):
        self.assertTrue(matches_query("is:foil", BIRDS_OF_PARADISE))
        self.assertFalse(matches_query("is:foil", LIGHTNING_BOLT))


# ---------------------------------------------------------------------------
# Evaluator Tests — otag (oracle tags)
# ---------------------------------------------------------------------------

class TestEvalOtag(unittest.TestCase):

    def setUp(self):
        self.otag_cache = {
            "ramp": {"birds-oracle-002", "solring-oracle-006"},
            "removal": {"bolt-oracle-001", "wrath-oracle-003"},
        }

    def test_otag_match(self):
        ast = parse_query("otag:ramp")
        self.assertTrue(evaluate_query(ast, BIRDS_OF_PARADISE, self.otag_cache))
        self.assertTrue(evaluate_query(ast, SOL_RING, self.otag_cache))

    def test_otag_no_match(self):
        ast = parse_query("otag:ramp")
        self.assertFalse(evaluate_query(ast, LIGHTNING_BOLT, self.otag_cache))

    def test_otag_removal(self):
        ast = parse_query("otag:removal")
        self.assertTrue(evaluate_query(ast, LIGHTNING_BOLT, self.otag_cache))
        self.assertTrue(evaluate_query(ast, WRATH_OF_GOD, self.otag_cache))
        self.assertFalse(evaluate_query(ast, BIRDS_OF_PARADISE, self.otag_cache))

    def test_otag_no_cache(self):
        """Without cache, otag queries return False."""
        ast = parse_query("otag:ramp")
        self.assertFalse(evaluate_query(ast, BIRDS_OF_PARADISE))

    def test_otag_unknown_tag(self):
        ast = parse_query("otag:nonexistent")
        self.assertFalse(evaluate_query(ast, BIRDS_OF_PARADISE, self.otag_cache))


# ---------------------------------------------------------------------------
# Evaluator Tests — Compound Queries
# ---------------------------------------------------------------------------

class TestEvalCompound(unittest.TestCase):

    def test_and(self):
        """c:w t:creature => must be white AND creature."""
        # Wrath is white sorcery, not creature
        self.assertFalse(matches_query("c:w t:creature", WRATH_OF_GOD))

    def test_or(self):
        """t:instant or t:sorcery => either type matches."""
        self.assertTrue(matches_query("t:instant or t:sorcery", LIGHTNING_BOLT))
        self.assertTrue(matches_query("t:instant or t:sorcery", WRATH_OF_GOD))
        self.assertFalse(matches_query("t:instant or t:sorcery", BIRDS_OF_PARADISE))

    def test_not(self):
        """-t:land => not a land."""
        self.assertTrue(matches_query("-t:land", LIGHTNING_BOLT))
        self.assertFalse(matches_query("-t:land", FOREST))

    def test_complex_and_or_not(self):
        """(c:r or c:g) -t:land cmc<=3"""
        query = "(c:r or c:g) -t:land cmc<=3"
        self.assertTrue(matches_query(query, LIGHTNING_BOLT))   # red, not land, cmc=1
        self.assertTrue(matches_query(query, BIRDS_OF_PARADISE))  # green, not land, cmc=1
        self.assertFalse(matches_query(query, FOREST))          # green but IS a land
        self.assertFalse(matches_query(query, WRATH_OF_GOD))    # white, not r/g
        self.assertFalse(matches_query(query, NICOL_BOLAS))     # red but cmc=4

    def test_double_negation_query(self):
        """-t:land -t:creature => not land AND not creature."""
        query = "-t:land -t:creature"
        self.assertTrue(matches_query(query, LIGHTNING_BOLT))   # instant
        self.assertTrue(matches_query(query, WRATH_OF_GOD))     # sorcery
        self.assertTrue(matches_query(query, SOL_RING))         # artifact
        self.assertFalse(matches_query(query, BIRDS_OF_PARADISE))  # creature
        self.assertFalse(matches_query(query, FOREST))          # land

    def test_price_tier(self):
        """usd>=5 usd<10"""
        query = "usd>=5 usd<10"
        self.assertTrue(matches_query(query, BIRDS_OF_PARADISE))  # $8
        self.assertTrue(matches_query(query, WRATH_OF_GOD))       # $5.25
        self.assertFalse(matches_query(query, NICOL_BOLAS))       # $15
        self.assertFalse(matches_query(query, LIGHTNING_BOLT))    # $1.50

    def test_edh_staple_query(self):
        """usd>=5 legal:commander"""
        query = "usd>=5 legal:commander"
        self.assertTrue(matches_query(query, NICOL_BOLAS))
        self.assertTrue(matches_query(query, BIRDS_OF_PARADISE))
        self.assertTrue(matches_query(query, WRATH_OF_GOD))
        self.assertFalse(matches_query(query, LIGHTNING_BOLT))  # $1.50


# ---------------------------------------------------------------------------
# collect_otag_terms Tests
# ---------------------------------------------------------------------------

class TestCollectOtags(unittest.TestCase):

    def test_single_otag(self):
        ast = parse_query("otag:ramp")
        self.assertEqual(collect_otag_terms(ast), {"ramp"})

    def test_multiple_otags(self):
        ast = parse_query("otag:ramp or otag:removal")
        self.assertEqual(collect_otag_terms(ast), {"ramp", "removal"})

    def test_otag_in_and(self):
        ast = parse_query("otag:ramp legal:commander")
        self.assertEqual(collect_otag_terms(ast), {"ramp"})

    def test_negated_otag(self):
        ast = parse_query("-otag:ramp")
        self.assertEqual(collect_otag_terms(ast), {"ramp"})

    def test_no_otags(self):
        ast = parse_query("c:w t:creature")
        self.assertEqual(collect_otag_terms(ast), set())

    def test_nested_otags(self):
        ast = parse_query("(otag:ramp or otag:removal) otag:card-advantage")
        self.assertEqual(collect_otag_terms(ast),
                         {"ramp", "removal", "card-advantage"})


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def test_none_card_data(self):
        """Evaluating against None card data should not crash."""
        ast = parse_query("c:w")
        # Should handle gracefully — colors field won't exist
        result = evaluate_query(ast, {})
        self.assertFalse(result)

    def test_missing_prices(self):
        card = {"name": "Test", "prices": {}}
        self.assertFalse(matches_query("usd>=1", card))

    def test_missing_prices_key(self):
        card = {"name": "Test"}
        self.assertFalse(matches_query("usd>=1", card))

    def test_star_power(self):
        """Power of '*' should not match numeric queries."""
        card = {"name": "Test", "power": "*", "toughness": "*"}
        self.assertFalse(matches_query("pow:0", card))
        self.assertFalse(matches_query("tou>=1", card))

    def test_case_insensitive_type(self):
        self.assertTrue(matches_query("t:Creature", BIRDS_OF_PARADISE))
        self.assertTrue(matches_query("t:LAND", FOREST))

    def test_case_insensitive_oracle(self):
        self.assertTrue(matches_query("o:Damage", LIGHTNING_BOLT))


if __name__ == '__main__':
    unittest.main()
