"""
Tests for the `edhrec-top:N` query predicate and its supporting
infrastructure (card_rankings table, EDHRECSource ranking computation).

Three layers:

1. Tokenizer accepts `edhrec-top` (hyphen in field name) and tokens
   round-trip with the supported operators.
2. The evaluator returns the correct truth for the various rank/op
   combinations.
3. The EDHRECSource helpers compute ranks correctly from per-card data
   and write them atomically (replacing stale entries).
"""

import os
import sqlite3
import tempfile
import unittest
import unittest.mock as mock


class TokenizerTests(unittest.TestCase):
    def setUp(self):
        from query_parser import tokenize, Token  # noqa: E402
        self.tokenize = tokenize
        self.Token = Token

    def test_field_with_hyphen_tokenizes(self):
        toks = self.tokenize('edhrec-top:1000')
        self.assertEqual(len(toks), 1)
        self.assertEqual(toks[0].type, 'FIELD_QUERY')
        field, op, val = toks[0].value
        self.assertEqual(field, 'edhrec_top')  # canonical form
        self.assertEqual(op, ':')
        self.assertEqual(val, '1000')

    def test_field_with_comparison_op(self):
        toks = self.tokenize('edhrec-top<=500')
        self.assertEqual(len(toks), 1)
        field, op, val = toks[0].value
        self.assertEqual(field, 'edhrec_top')
        self.assertEqual(op, '<=')
        self.assertEqual(val, '500')

    def test_underscore_form_alias_works(self):
        toks = self.tokenize('edhrec_top:100')
        self.assertEqual(toks[0].value[0], 'edhrec_top')

    def test_negation_works(self):
        # NOT prefix + hyphenated field should both parse
        toks = self.tokenize('-edhrec-top:1000')
        self.assertEqual(toks[0].type, 'NOT')
        self.assertEqual(toks[1].type, 'FIELD_QUERY')
        self.assertEqual(toks[1].value[0], 'edhrec_top')


class PredicateEvalTests(unittest.TestCase):
    """Test the predicate via parse_query + evaluate_query end-to-end."""

    def setUp(self):
        from query_parser import parse_query, evaluate_query  # noqa: E402
        self.parse_query = parse_query
        self.evaluate_query = evaluate_query
        self.card = {'name': 'Sol Ring', 'oracle_id': 'sol-ring-id'}

    def _eval(self, query, rank=None):
        ast = self.parse_query(query)
        enr = {'edhrec_top_rank': rank} if rank is not None else {}
        return self.evaluate_query(ast, self.card, enrichment_data=enr)

    def test_top_n_matches_rank_within(self):
        self.assertTrue(self._eval('edhrec-top:1000', rank=42))
        self.assertTrue(self._eval('edhrec-top:1000', rank=1000))  # boundary

    def test_top_n_rejects_rank_above(self):
        self.assertFalse(self._eval('edhrec-top:1000', rank=1001))
        self.assertFalse(self._eval('edhrec-top:100', rank=1500))

    def test_strict_less_than(self):
        # rank<100 means rank in [1, 99]
        self.assertTrue(self._eval('edhrec-top<100', rank=50))
        self.assertFalse(self._eval('edhrec-top<100', rank=100))  # not strict

    def test_greater_than_finds_outside_top_n(self):
        # rank>500 means "outside top 500"
        self.assertTrue(self._eval('edhrec-top>500', rank=501))
        self.assertFalse(self._eval('edhrec-top>500', rank=500))
        self.assertFalse(self._eval('edhrec-top>500', rank=10))

    def test_unranked_card_never_matches(self):
        # Cards with no rank in enrichment_data don't match — they're
        # missing data, not implicitly "outside the top."
        self.assertFalse(self._eval('edhrec-top:1000', rank=None))
        # Even for rank>N which would intuitively mean "lots of cards"
        self.assertFalse(self._eval('edhrec-top>500', rank=None))

    def test_no_enrichment_data_falls_through_false(self):
        ast = self.parse_query('edhrec-top:1000')
        # No enrichment_data argument at all
        result = self.evaluate_query(ast, self.card)
        self.assertFalse(result)

    def test_negation_excludes_top_n(self):
        # -edhrec-top:1000 should pick cards NOT in top 1000.
        # But unranked cards still don't match anything (the inner
        # predicate returns False, NOT-flipped to True). That means
        # NOT-edhrec-top:1000 matches cards with rank > 1000 AND cards
        # with no rank — possibly surprising behavior the user should
        # know about.
        self.assertFalse(self._eval('-edhrec-top:1000', rank=42))
        self.assertTrue(self._eval('-edhrec-top:1000', rank=1500))
        # Unranked → predicate False → -predicate True
        self.assertTrue(self._eval('-edhrec-top:1000', rank=None))


class ComputeRankingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The EDHREC source pulls heavy deps; mock at the module level
        # if anything blocks import.
        from web_enrichment import edhrec
        cls.EDHRECSource = edhrec.EDHRECSource

    def test_basic_ordering_by_inclusion_pct(self):
        name_map = {'A': 'oid-a', 'B': 'oid-b', 'C': 'oid-c'}
        # detail pages: A is 50%, B is 30%, C is 70%
        card_details = {
            'a-slug': {'name': 'A', 'num_decks': 50,  'potential_decks': 100},
            'b-slug': {'name': 'B', 'num_decks': 30,  'potential_decks': 100},
            'c-slug': {'name': 'C', 'num_decks': 70,  'potential_decks': 100},
        }
        rows = self.EDHRECSource._compute_rankings(
            top_cards=[], card_details=card_details, name_map=name_map
        )
        # Expect rank 1 = C, rank 2 = A, rank 3 = B
        self.assertEqual([r['oracle_id'] for r in rows],
                         ['oid-c', 'oid-a', 'oid-b'])
        self.assertEqual([r['rank'] for r in rows], [1, 2, 3])

    def test_top_year_supplies_when_detail_missing(self):
        name_map = {'A': 'oid-a', 'B': 'oid-b'}
        # A has only top_cards entry, B has only detail entry
        rows = self.EDHRECSource._compute_rankings(
            top_cards=[
                {'name': 'A', 'num_decks': 80, 'potential_decks': 100},
            ],
            card_details={
                'b-slug': {'name': 'B', 'num_decks': 50, 'potential_decks': 100},
            },
            name_map=name_map,
        )
        self.assertEqual([r['oracle_id'] for r in rows],
                         ['oid-a', 'oid-b'])

    def test_detail_page_wins_when_both_present(self):
        # A appears in both with different inclusion — best wins.
        name_map = {'A': 'oid-a'}
        rows = self.EDHRECSource._compute_rankings(
            top_cards=[{'name': 'A', 'num_decks': 30, 'potential_decks': 100}],
            card_details={
                'a-slug': {'name': 'A', 'num_decks': 70, 'potential_decks': 100},
            },
            name_map=name_map,
        )
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['score'], 0.7)

    def test_unmapped_names_skipped(self):
        # Card names not in name_map → no oracle_id → skip.
        name_map = {'Known': 'oid-known'}
        rows = self.EDHRECSource._compute_rankings(
            top_cards=[
                {'name': 'Known', 'num_decks': 10, 'potential_decks': 100},
                {'name': 'Unknown', 'num_decks': 90, 'potential_decks': 100},
            ],
            card_details={},
            name_map=name_map,
        )
        self.assertEqual([r['oracle_id'] for r in rows], ['oid-known'])

    def test_zero_potential_skipped(self):
        # Defensive: don't divide by zero.
        name_map = {'A': 'oid-a'}
        rows = self.EDHRECSource._compute_rankings(
            top_cards=[{'name': 'A', 'num_decks': 0, 'potential_decks': 0}],
            card_details={},
            name_map=name_map,
        )
        self.assertEqual(rows, [])

    def test_deterministic_tiebreak(self):
        # Equal scores → tiebreak by oracle_id ascending so output is stable.
        name_map = {'A': 'oid-z', 'B': 'oid-a'}
        rows = self.EDHRECSource._compute_rankings(
            top_cards=[],
            card_details={
                'a-slug': {'name': 'A', 'num_decks': 50, 'potential_decks': 100},
                'b-slug': {'name': 'B', 'num_decks': 50, 'potential_decks': 100},
            },
            name_map=name_map,
        )
        # Same score; tiebreak: oid-a before oid-z lexically
        self.assertEqual([r['oracle_id'] for r in rows], ['oid-a', 'oid-z'])


class WriteRankingsTests(unittest.TestCase):
    """Verify _write_rankings atomically replaces stale rows."""

    @classmethod
    def setUpClass(cls):
        from web_enrichment import edhrec
        cls.EDHRECSource = edhrec.EDHRECSource

    def setUp(self):
        # Ephemeral DB with the card_rankings schema only
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE card_rankings (
                oracle_id    TEXT NOT NULL,
                source       TEXT NOT NULL,
                rank         INTEGER NOT NULL,
                score        REAL,
                last_updated TEXT,
                PRIMARY KEY (oracle_id, source)
            );
        """)

    def tearDown(self):
        self.conn.close()
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _all(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM card_rankings ORDER BY rank").fetchall()]

    def test_initial_write(self):
        rows = [
            {'oracle_id': 'a', 'source': 'edhrec', 'rank': 1,
             'score': 0.9, 'last_updated': 't1'},
            {'oracle_id': 'b', 'source': 'edhrec', 'rank': 2,
             'score': 0.5, 'last_updated': 't1'},
        ]
        n = self.EDHRECSource._write_rankings(self.conn, rows)
        self.assertEqual(n, 2)
        self.assertEqual(len(self._all()), 2)

    def test_replace_drops_stale(self):
        # First refresh: 3 cards
        first = [
            {'oracle_id': 'a', 'source': 'edhrec', 'rank': 1,
             'score': 0.9, 'last_updated': 't1'},
            {'oracle_id': 'b', 'source': 'edhrec', 'rank': 2,
             'score': 0.5, 'last_updated': 't1'},
            {'oracle_id': 'c', 'source': 'edhrec', 'rank': 3,
             'score': 0.3, 'last_updated': 't1'},
        ]
        self.EDHRECSource._write_rankings(self.conn, first)
        # Second refresh: 'c' dropped out
        second = [
            {'oracle_id': 'a', 'source': 'edhrec', 'rank': 1,
             'score': 0.95, 'last_updated': 't2'},
            {'oracle_id': 'b', 'source': 'edhrec', 'rank': 2,
             'score': 0.6, 'last_updated': 't2'},
        ]
        self.EDHRECSource._write_rankings(self.conn, second)
        all_rows = self._all()
        self.assertEqual(len(all_rows), 2,
                         "Stale 'c' row must be wiped, not kept around")
        self.assertEqual([r['oracle_id'] for r in all_rows], ['a', 'b'])

    def test_other_source_untouched(self):
        # Pre-populate a different-source row; it must survive an EDHREC
        # rewrite.
        self.conn.execute(
            """INSERT INTO card_rankings
               (oracle_id, source, rank, score, last_updated)
               VALUES (?, ?, ?, ?, ?)""",
            ('x', 'edhtop16', 5, 0.1, 't0')
        )
        self.conn.commit()
        self.EDHRECSource._write_rankings(self.conn, [
            {'oracle_id': 'a', 'source': 'edhrec', 'rank': 1,
             'score': 0.9, 'last_updated': 't1'},
        ])
        all_rows = self._all()
        self.assertEqual(len(all_rows), 2)
        sources = sorted({r['source'] for r in all_rows})
        self.assertEqual(sources, ['edhrec', 'edhtop16'])

    def test_empty_input_is_noop(self):
        # Pre-populate something
        self.conn.execute(
            """INSERT INTO card_rankings VALUES (?, ?, ?, ?, ?)""",
            ('a', 'edhrec', 1, 0.9, 't1')
        )
        self.conn.commit()
        n = self.EDHRECSource._write_rankings(self.conn, [])
        self.assertEqual(n, 0)
        # Empty input should NOT wipe existing rows — caller is saying
        # "I have nothing to update," not "delete everything."
        self.assertEqual(len(self._all()), 1)


if __name__ == '__main__':
    unittest.main()
