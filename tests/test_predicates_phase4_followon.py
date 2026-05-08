"""
Tests for the Phase 4 follow-on predicates:
- name>=M (and other comparison operators on the name field)
- edhtop16-top:N (cEDH rank from the edhtop16 enrichment source)

The existing edhrec-top tests cover the rank-predicate machinery; these
tests pin the additions without re-asserting the shared behavior.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock as mock


def _install_hw_mocks():
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.ser = None
        sys.modules['gcode_control'] = gcode_mock


class NameRangeTests(unittest.TestCase):
    """name>=M / name<m / name>=z and friends. Substring-via-`:` is
    unchanged — covered in test_query_parser.py."""

    def setUp(self):
        _install_hw_mocks()
        from query_parser import parse_query, evaluate_query
        self.parse_query = parse_query
        self.evaluate_query = evaluate_query

    def _eval(self, query, name):
        ast = self.parse_query(query)
        return self.evaluate_query(ast, {'name': name})

    def test_substring_via_colon_unchanged(self):
        # The pre-existing semantics: `name:sol` matches "Sol Ring".
        self.assertTrue(self._eval('name:sol', 'Sol Ring'))
        self.assertTrue(self._eval('name:sol', 'Soldevi Excavations'))
        self.assertFalse(self._eval('name:sol', 'Lightning Bolt'))

    def test_gte_lex(self):
        # name>=m matches Mountain, Ravager, Zealous Conscripts; not Counterspell.
        self.assertTrue(self._eval('name>=m', 'Mountain'))
        self.assertTrue(self._eval('name>=m', 'Ravager'))
        self.assertTrue(self._eval('name>=m', 'Zealous Conscripts'))
        self.assertFalse(self._eval('name>=m', 'Counterspell'))

    def test_lt_lex(self):
        # name<m matches Counterspell, Lightning Bolt; not Mountain, Z*.
        self.assertTrue(self._eval('name<m', 'Counterspell'))
        self.assertTrue(self._eval('name<m', 'Lightning Bolt'))
        self.assertFalse(self._eval('name<m', 'Mountain'))
        self.assertFalse(self._eval('name<m', 'Zealous Conscripts'))

    def test_partition_pair_is_complete_and_disjoint(self):
        # name<m  AND  name>=m  must partition the name space cleanly.
        for name in ('Aether Vial', 'Lightning Bolt', 'Mountain',
                     'Sol Ring', 'Zealous Conscripts'):
            lhs = self._eval('name<m', name)
            rhs = self._eval('name>=m', name)
            self.assertNotEqual(lhs, rhs,
                                f'{name} matched both / neither: lhs={lhs} rhs={rhs}')

    def test_lte_and_gt(self):
        self.assertTrue(self._eval('name<=mountain', 'Mountain'))
        self.assertFalse(self._eval('name<mountain', 'Mountain'))
        self.assertTrue(self._eval('name>m', 'Mountain'))
        self.assertFalse(self._eval('name>=z', 'Mountain'))

    def test_case_insensitive(self):
        # The user shouldn't have to think about case.
        self.assertTrue(self._eval('name>=M', 'mountain'))
        self.assertTrue(self._eval('name>=m', 'Mountain'))


class EdhTop16TopPredicateTests(unittest.TestCase):
    """edhtop16-top:N reads from enrichment_data.edhtop16_top_rank."""

    def setUp(self):
        _install_hw_mocks()
        from query_parser import parse_query, evaluate_query
        self.parse_query = parse_query
        self.evaluate_query = evaluate_query

    def _eval(self, query, rank=None, edhrec_rank=None):
        ast = self.parse_query(query)
        enr = {}
        if rank is not None: enr['edhtop16_top_rank'] = rank
        if edhrec_rank is not None: enr['edhrec_top_rank'] = edhrec_rank
        return self.evaluate_query(ast, {'name': 'X'}, enrichment_data=enr)

    def test_alias_forms_all_resolve(self):
        # All four aliases land on the same canonical field.
        for q in ('edhtop16-top:50', 'edhtop16_top:50',
                  'cedh-top:50', 'cedh_top:50'):
            self.assertTrue(self._eval(q, rank=10),
                            f'{q} should match a rank-10 card')

    def test_in_top_n(self):
        self.assertTrue(self._eval('edhtop16-top:50', rank=49))
        self.assertTrue(self._eval('edhtop16-top:50', rank=50))  # boundary
        self.assertFalse(self._eval('edhtop16-top:50', rank=51))

    def test_unranked_card_never_matches(self):
        self.assertFalse(self._eval('edhtop16-top:50', rank=None))

    def test_independent_from_edhrec_rank(self):
        # A card ranked in EDHREC's top 100 but absent from edhtop16
        # should not be matched by edhtop16-top.
        self.assertFalse(self._eval('edhtop16-top:50',
                                    rank=None, edhrec_rank=42))

    def test_combined_query(self):
        # `edhtop16-top:50 -edhrec-top:1000` finds cEDH staples that
        # AREN'T in the EDHREC top 1000 (cEDH-only mainstays).
        ast = self.parse_query('edhtop16-top:50 -edhrec-top:1000')
        # cEDH rank 30, EDHREC rank 5000 -> matches
        self.assertTrue(self.evaluate_query(
            ast, {'name': 'X'},
            enrichment_data={'edhtop16_top_rank': 30,
                             'edhrec_top_rank': 5000},
        ))
        # cEDH rank 30, EDHREC rank 100 -> doesn't match (in EDHREC top 1000)
        self.assertFalse(self.evaluate_query(
            ast, {'name': 'X'},
            enrichment_data={'edhtop16_top_rank': 30,
                             'edhrec_top_rank': 100},
        ))


class EdhTop16ComputeRankingsTests(unittest.TestCase):
    """EDHTop16Source._compute_rankings — sort + tiebreak + score math."""

    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        from web_enrichment import edhtop16
        cls.Source = edhtop16.EDHTop16Source

    def test_basic_ordering(self):
        # 3 cards, 100 total entries; A=80, B=30, C=50 → C is rank 1? No:
        # A is highest (80), C next (50), B last (30).
        rows = self.Source._compute_rankings(
            {'a': 80, 'b': 30, 'c': 50}, total_entries=100,
        )
        self.assertEqual([r['oracle_id'] for r in rows], ['a', 'c', 'b'])
        self.assertEqual([r['rank'] for r in rows], [1, 2, 3])
        self.assertAlmostEqual(rows[0]['score'], 0.8)

    def test_zero_count_skipped(self):
        rows = self.Source._compute_rankings(
            {'a': 0, 'b': 5}, total_entries=10,
        )
        self.assertEqual([r['oracle_id'] for r in rows], ['b'])

    def test_zero_total_entries_safe(self):
        # Don't divide by zero. Default to 1 in the denominator so
        # at least the ordering is preserved by raw count.
        rows = self.Source._compute_rankings(
            {'a': 5, 'b': 2}, total_entries=0,
        )
        self.assertEqual([r['oracle_id'] for r in rows], ['a', 'b'])

    def test_deterministic_tiebreak(self):
        rows = self.Source._compute_rankings(
            {'oid-z': 50, 'oid-a': 50}, total_entries=100,
        )
        self.assertEqual([r['oracle_id'] for r in rows], ['oid-a', 'oid-z'])


class EdhTop16WriteRankingsTests(unittest.TestCase):
    """_write_rankings: atomic replace, leaves other sources alone."""

    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        from web_enrichment import edhtop16
        cls.Source = edhtop16.EDHTop16Source

    def setUp(self):
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

    def test_replace_clears_only_edhtop16_rows(self):
        # Pre-populate edhrec rows.
        self.conn.execute(
            """INSERT INTO card_rankings
               (oracle_id, source, rank, score, last_updated)
               VALUES (?, ?, ?, ?, ?)""",
            ('a', 'edhrec', 1, 0.9, 't0'),
        )
        # Pre-populate stale edhtop16 rows that should be dropped.
        self.conn.execute(
            """INSERT INTO card_rankings
               VALUES (?, ?, ?, ?, ?)""",
            ('stale', 'edhtop16', 1, 0.8, 't0'),
        )
        self.conn.commit()

        self.Source._write_rankings(self.conn, [
            {'oracle_id': 'a', 'source': 'edhtop16', 'rank': 1,
             'score': 0.5, 'last_updated': 't1'},
        ])

        rows = self.conn.execute(
            "SELECT oracle_id, source FROM card_rankings ORDER BY source, oracle_id"
        ).fetchall()
        # edhrec row preserved; edhtop16 stale row gone; new edhtop16 row added.
        self.assertEqual(
            [(r['oracle_id'], r['source']) for r in rows],
            [('a', 'edhrec'), ('a', 'edhtop16')],
        )


if __name__ == '__main__':
    unittest.main()
