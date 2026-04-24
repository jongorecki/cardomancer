# tests/integrations/test_collection_filter_endpoint.py
# ---------------------------------------------------------------------------
# Integration tests for /api/collection/filter (Phase 2 item 2.13).
#
# The Collection tab's filter chips compile to a single query string and
# hit this endpoint. These tests confirm the endpoint:
#   * returns the full inventory when q is empty
#   * filters by supported tokens (usd<X, t:creature, name:)
#   * paginates correctly
#   * returns a 400 on query parse errors
#   * surfaces the compiled query back in the response
#
# UI-behavior reminder (no browser test coverage here):
#   - Toggling a chip should call updateCollectionFilter() in static/app.js
#   - The "Compiled query:" preview should match the `q` sent to this
#     endpoint.
#   - "Any staple" chip is exclusive and clears its siblings.
#
# Some enrichment tokens (staple:, buylist:, salt>, combo:, cull:) depend
# on the query_parser version shipped alongside this worktree; if they
# aren't registered yet the endpoint will cleanly return a 400 with an
# "Unknown field" message. The chip-compilation tests exercise those
# tokens in the UI layer instead (_compileCollectionQuery()) and do not
# hit the endpoint.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import collection_db
import enrichment_db
import web_server


def _seed_inventory(conn):
    """Insert a handful of cards spanning type, price, and color."""
    # (name, set, cn, colors, cmc, type_line, rarity, price, qty)
    rows = [
        ("Sol Ring",         "cmr", "283", "",   1.0, "Artifact",
         "uncommon", 2.50, 1),
        ("Lightning Bolt",   "m11", "149", "R",  1.0, "Instant",
         "common",   0.50, 2),
        ("Counterspell",     "lea", "054", "U",  2.0, "Instant",
         "uncommon", 5.00, 1),
        ("Birds of Paradise","m12", "165", "G",  1.0, "Creature — Bird",
         "rare",     8.00, 1),
        ("Llanowar Elves",   "m12", "182", "G",  1.0, "Creature — Elf Druid",
         "common",   0.25, 4),
    ]
    now = "2026-04-23T00:00:00"
    for r in rows:
        conn.execute(
            """INSERT INTO inventory
               (name, set_code, collector_number, colors, cmc, type_line,
                rarity, price_usd, quantity, first_scanned, last_scanned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*r, now, now),
        )
    conn.commit()


class TestCollectionFilterEndpoint(unittest.TestCase):

    def setUp(self):
        # Temp collection.db + enrichment.db with schema created.
        self.tmpdir = tempfile.mkdtemp(prefix="collection_filter_test_")
        self.coll_path = os.path.join(self.tmpdir, "collection.db")
        self.enr_path  = os.path.join(self.tmpdir, "enrichment.db")

        self.coll_conn = collection_db.get_connection(db_path=self.coll_path)
        _seed_inventory(self.coll_conn)

        # Create enrichment schema (empty data is fine for these tests).
        enr_init = enrichment_db.get_connection(db_path=self.enr_path)
        enr_init.close()

        # Patch both get_connection helpers so the endpoint's
        # `get_connection()` calls land on our temp DBs.
        self._orig_coll_get = collection_db.get_connection
        self._orig_enr_get  = enrichment_db.get_connection

        def _coll_patch(db_path=None):
            return self._orig_coll_get(db_path=self.coll_path)

        def _enr_patch(db_path=None):
            return self._orig_enr_get(db_path=self.enr_path)

        collection_db.get_connection = _coll_patch
        enrichment_db.get_connection = _enr_patch

        self.client = web_server.app.test_client()

    def tearDown(self):
        collection_db.get_connection = self._orig_coll_get
        enrichment_db.get_connection = self._orig_enr_get
        try:
            self.coll_conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- Basic behavior ----

    def test_empty_query_returns_full_inventory(self):
        rv = self.client.get('/api/collection/filter')
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertEqual(data['total'], 5)
        self.assertEqual(len(data['items']), 5)
        self.assertEqual(data.get('query', ''), '')

    def test_query_echoed_back_in_response(self):
        rv = self.client.get('/api/collection/filter?q=usd%3C1.00')
        data = rv.get_json()
        self.assertEqual(data['query'], 'usd<1.00')

    # ---- Single-token filters (chip simulation) ----

    def test_usd_under_1_matches_two(self):
        # Bolt $0.50 + Llanowar $0.25 = 2 rows
        rv = self.client.get('/api/collection/filter?q=usd%3C1.00')
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertEqual(data['total'], 2)
        names = {it['name'] for it in data['items']}
        self.assertEqual(names, {"Lightning Bolt", "Llanowar Elves"})

    def test_usd_5_plus_matches_two(self):
        # Counterspell $5.00 + Birds $8.00 = 2 rows (>= 5)
        rv = self.client.get('/api/collection/filter?q=usd%3E%3D5.00')
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertEqual(data['total'], 2)
        names = {it['name'] for it in data['items']}
        self.assertEqual(names, {"Counterspell", "Birds of Paradise"})

    def test_type_creature_matches_two(self):
        rv = self.client.get('/api/collection/filter?q=t%3Acreature')
        data = rv.get_json()
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(data['total'], 2)
        names = {it['name'] for it in data['items']}
        self.assertEqual(names, {"Birds of Paradise", "Llanowar Elves"})

    # ---- Multi-group AND composition (what chip compilation emits) ----

    def test_and_of_two_groups(self):
        # "t:creature usd<1.00" -> only Llanowar Elves ($0.25 creature)
        rv = self.client.get('/api/collection/filter?q=t%3Acreature+usd%3C1.00')
        data = rv.get_json()
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['name'], "Llanowar Elves")

    def test_or_within_group(self):
        # "(usd<1.00 or usd>=5.00)" -> Bolt, Llanowar, Counterspell, Birds
        rv = self.client.get(
            '/api/collection/filter?q=%28usd%3C1.00+or+usd%3E%3D5.00%29'
        )
        data = rv.get_json()
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(data['total'], 4)
        names = {it['name'] for it in data['items']}
        self.assertEqual(names, {
            "Lightning Bolt", "Llanowar Elves",
            "Counterspell", "Birds of Paradise",
        })

    # ---- Errors + pagination ----

    def test_parse_error_returns_400(self):
        rv = self.client.get('/api/collection/filter?q=bogus%3Afield')
        self.assertEqual(rv.status_code, 400)
        data = rv.get_json()
        self.assertIn('error', data)

    def test_pagination(self):
        rv = self.client.get(
            '/api/collection/filter?q=&page=1&per_page=2'
        )
        data = rv.get_json()
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(data['total'], 5)
        self.assertEqual(len(data['items']), 2)
        self.assertEqual(data['pages'], 3)
        self.assertEqual(data['page'], 1)

        rv2 = self.client.get(
            '/api/collection/filter?q=&page=3&per_page=2'
        )
        data2 = rv2.get_json()
        self.assertEqual(len(data2['items']), 1)


if __name__ == "__main__":
    unittest.main()
