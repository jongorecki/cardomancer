"""Integration tests for the Phase 2.11 physical-locator endpoint
(`GET /api/collection/locate`) and the underlying
`collection_db.locate_cards_by_query` helper.

Tests cover:
  * query returns cards from multiple boxes, grouped correctly
  * cards with no divider fall back to the legacy inventory.box text
  * cards with neither divider nor box land in "Unassigned"
  * empty query returns 400 (matches existing API conventions)
  * the endpoint is wired up and responds with the expected JSON shape
"""

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import collection_db


def _seed(conn, name, set_code="tst", colors="R", cmc=1.0,
          rarity="common", type_line="Instant", price=0.1, quantity=1,
          oracle_id=None, box=None, divider_id=None):
    """Insert an inventory row and optionally attach a divider."""
    # Ensure unique (name, set, collector_number) so inserts don't collide.
    oid = oracle_id or f"oid-{name}-{set_code}"
    now = "2026-01-01T00:00:00"
    conn.execute(
        """INSERT INTO inventory
           (name, set_code, collector_number, oracle_id, colors, cmc,
            type_line, rarity, price_usd, quantity, first_scanned,
            last_scanned, box, divider_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, set_code, name.lower().replace(' ', '-'), oid, colors, cmc,
         type_line, rarity, price, quantity, now, now, box, divider_id)
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestLocateCardsByQueryCore(unittest.TestCase):
    """Direct tests against collection_db.locate_cards_by_query."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_collection.db")
        self.conn = collection_db.get_connection(db_path=self.db_path)

        # Two boxes with three dividers between them.
        self.box_a = collection_db.add_box(self.conn, "Alpha")
        self.box_b = collection_db.add_box(self.conn, "Beta")
        self.div_a1 = collection_db.add_divider(
            self.conn, self.box_a, "Red", 1
        )
        self.div_a2 = collection_db.add_divider(
            self.conn, self.box_a, "Blue", 2
        )
        self.div_b1 = collection_db.add_divider(
            self.conn, self.box_b, "Red-B", 1
        )

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_groups_cards_across_boxes(self):
        # Two red cards in Alpha/Red, one red card in Beta/Red-B.
        _seed(self.conn, "Bolt One", colors="R", divider_id=self.div_a1)
        _seed(self.conn, "Bolt Two", colors="R", divider_id=self.div_a1,
              quantity=2)
        _seed(self.conn, "Bolt Three", colors="R", divider_id=self.div_b1)
        # A blue card that should NOT match c:r.
        _seed(self.conn, "Counterspell", colors="U", divider_id=self.div_a2)

        groups = collection_db.locate_cards_by_query(self.conn, "c:r")

        # Two buckets: (Alpha, Red) and (Beta, Red-B).
        self.assertEqual(len(groups), 2)
        by_box = {g['box_name']: g for g in groups}
        self.assertEqual(set(by_box.keys()), {"Alpha", "Beta"})
        self.assertEqual(by_box['Alpha']['divider_label'], "Red")
        self.assertEqual(by_box['Alpha']['count'], 3)  # 1 + 2 qty
        self.assertEqual(by_box['Alpha']['unique_cards'], 2)
        self.assertEqual(by_box['Beta']['divider_label'], "Red-B")
        self.assertEqual(by_box['Beta']['count'], 1)

        # Blue card is NOT in any returned bucket.
        all_names = []
        for g in groups:
            # oracle_ids is a proxy for which cards ended up in the group.
            all_names.extend(g['oracle_ids'])
        self.assertNotIn("oid-Counterspell-tst", all_names)

    def test_dividers_sorted_by_position_within_box(self):
        _seed(self.conn, "Second", colors="R", divider_id=self.div_a2)
        _seed(self.conn, "First", colors="R", divider_id=self.div_a1)

        # Add a divider with position=0 that precedes Red (position 1)
        # and confirm ordering is by position ASC.
        div_a0 = collection_db.add_divider(
            self.conn, self.box_a, "Zeroth", 0
        )
        _seed(self.conn, "Zero", colors="R", divider_id=div_a0)

        groups = collection_db.locate_cards_by_query(self.conn, "c:r")
        self.assertEqual([g['divider_label'] for g in groups],
                         ["Zeroth", "Red", "Blue"])

    def test_no_divider_falls_back_to_legacy_box_column(self):
        # Inventory.box = "Legacy Shoebox" but no divider_id.
        _seed(self.conn, "Loose Bolt", colors="R", box="Legacy Shoebox")
        # Another card with a real divider for contrast.
        _seed(self.conn, "Shelved", colors="R", divider_id=self.div_a1)

        groups = collection_db.locate_cards_by_query(self.conn, "c:r")
        by_box = {(g['box_name'], g['divider_label']): g for g in groups}
        self.assertIn(("Legacy Shoebox", None), by_box)
        legacy = by_box[("Legacy Shoebox", None)]
        self.assertEqual(legacy['count'], 1)
        self.assertIsNone(legacy['divider_id'])

    def test_no_divider_no_box_lands_in_unassigned(self):
        _seed(self.conn, "Orphan", colors="R")  # no box, no divider
        groups = collection_db.locate_cards_by_query(self.conn, "c:r")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['box_name'],
                         collection_db.UNASSIGNED_BOX_NAME)
        self.assertIsNone(groups[0]['divider_label'])
        self.assertIsNone(groups[0]['divider_id'])

    def test_unassigned_sorts_after_named_boxes(self):
        _seed(self.conn, "Orphan", colors="R")
        _seed(self.conn, "Shelved", colors="R", divider_id=self.div_a1)
        groups = collection_db.locate_cards_by_query(self.conn, "c:r")
        self.assertEqual(groups[-1]['box_name'],
                         collection_db.UNASSIGNED_BOX_NAME)

    def test_empty_query_returns_empty_list(self):
        _seed(self.conn, "Anything", colors="R", divider_id=self.div_a1)
        self.assertEqual(
            collection_db.locate_cards_by_query(self.conn, ""), []
        )
        self.assertEqual(
            collection_db.locate_cards_by_query(self.conn, "   "), []
        )

    def test_oracle_ids_are_unique_per_bucket(self):
        # Two rows with same oracle_id in same divider should only report
        # the id once in oracle_ids, but count reflects total quantity.
        _seed(self.conn, "DupeA", colors="R", divider_id=self.div_a1,
              oracle_id="shared-oid", quantity=2)
        _seed(self.conn, "DupeB", colors="R", divider_id=self.div_a1,
              oracle_id="shared-oid", quantity=1)
        groups = collection_db.locate_cards_by_query(self.conn, "c:r")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['count'], 3)
        self.assertEqual(groups[0]['oracle_ids'], ["shared-oid"])


class TestLocateEndpoint(unittest.TestCase):
    """Flask test-client smoke tests for GET /api/collection/locate."""

    @classmethod
    def setUpClass(cls):
        # Import web_server late — it's a heavy import (pulls in worker,
        # camera, etc.) so we share a single client across tests.
        import web_server
        cls.web_server = web_server
        cls.app = web_server.app
        cls.client = cls.app.test_client()

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "locator_endpoint.db")
        # Point collection_db at our temp DB so the endpoint uses it.
        self._orig_path = collection_db.DB_PATH
        collection_db.DB_PATH = self.db_path
        self.conn = collection_db.get_connection(db_path=self.db_path)

        self.box = collection_db.add_box(self.conn, "Endpoint Box")
        self.div = collection_db.add_divider(self.conn, self.box, "R1", 1)
        _seed(self.conn, "Endpoint Bolt", colors="R",
              divider_id=self.div, quantity=4)
        _seed(self.conn, "Legacy Bolt", colors="R", box="Shoebox")
        _seed(self.conn, "Orphan Bolt", colors="R")

    def tearDown(self):
        self.conn.close()
        collection_db.DB_PATH = self._orig_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_locate_returns_grouped_results(self):
        resp = self.client.get('/api/collection/locate?q=c:r')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['query'], 'c:r')
        # 4 (divider) + 1 (shoebox) + 1 (orphan)
        self.assertEqual(data['total_cards'], 6)
        box_names = [g['box_name'] for g in data['groups']]
        self.assertIn("Endpoint Box", box_names)
        self.assertIn("Shoebox", box_names)
        self.assertIn(collection_db.UNASSIGNED_BOX_NAME, box_names)

    def test_locate_empty_query_returns_400(self):
        resp = self.client.get('/api/collection/locate?q=')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())

    def test_locate_missing_query_returns_400(self):
        resp = self.client.get('/api/collection/locate')
        self.assertEqual(resp.status_code, 400)

    def test_locate_invalid_query_returns_400(self):
        # Unbalanced parenthesis — query_parser should raise.
        resp = self.client.get('/api/collection/locate?q=(c:r')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())

    def test_locate_no_matches_returns_empty_groups(self):
        # No green cards in the test data.
        resp = self.client.get('/api/collection/locate?q=c:g')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['total_cards'], 0)
        self.assertEqual(data['groups'], [])


class TestLocatorPostEndpoint(unittest.TestCase):
    """Tests for the POST /api/locator/query endpoint (Phase 2.11 AC path)."""

    @classmethod
    def setUpClass(cls):
        import web_server
        cls.web_server = web_server
        cls.app = web_server.app
        cls.client = cls.app.test_client()

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "locator_post.db")
        self._orig_path = collection_db.DB_PATH
        collection_db.DB_PATH = self.db_path
        self.conn = collection_db.get_connection(db_path=self.db_path)

        self.box = collection_db.add_box(self.conn, "Post Box")
        self.div = collection_db.add_divider(self.conn, self.box, "D1", 1)
        _seed(self.conn, "Post Bolt", colors="R",
              divider_id=self.div, quantity=3)
        _seed(self.conn, "Post Island", colors="U",
              box="Shoebox", quantity=1)
        _seed(self.conn, "Post Orphan", colors="G")

    def tearDown(self):
        self.conn.close()
        collection_db.DB_PATH = self._orig_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _post(self, body):
        return self.client.post(
            '/api/locator/query',
            json=body,
            content_type='application/json',
        )

    def test_post_returns_200_with_groups(self):
        """Happy path: query matches cards, response has correct shape."""
        resp = self._post({'q': 'c:r'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['query'], 'c:r')
        self.assertEqual(data['total_cards'], 3)
        self.assertEqual(len(data['groups']), 1)
        grp = data['groups'][0]
        self.assertEqual(grp['box_name'], 'Post Box')
        self.assertEqual(grp['divider_label'], 'D1')
        self.assertEqual(grp['count'], 3)
        self.assertIn('unique_cards', grp)
        self.assertIn('oracle_ids', grp)

    def test_post_multi_location_returns_all_groups(self):
        """A card owned in multiple locations shows up in every group."""
        # Seed the same oracle_id in two different locations.
        div2 = collection_db.add_divider(self.conn, self.box, "D2", 2)
        _seed(self.conn, "Multi Blue", colors="U",
              oracle_id="multi-oid", divider_id=self.div, quantity=1)
        _seed(self.conn, "Multi Blue Alt", colors="U",
              oracle_id="multi-oid", divider_id=div2, quantity=2)

        resp = self._post({'q': 'c:u'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        box_names  = [g['box_name']      for g in data['groups']]
        div_labels = [g['divider_label'] for g in data['groups']]
        # "Shoebox" (from setUp Island) and two dividers in Post Box.
        self.assertIn('Shoebox', box_names)
        self.assertIn('D1', div_labels)
        self.assertIn('D2', div_labels)

    def test_post_empty_query_returns_400(self):
        resp = self._post({'q': ''})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn('error', body)
        self.assertEqual(body['groups'], [])
        self.assertEqual(body['total_cards'], 0)

    def test_post_missing_q_returns_400(self):
        resp = self._post({})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())

    def test_post_invalid_query_returns_400(self):
        """Malformed query (bad field name) returns 400 with error."""
        resp = self._post({'q': 'badfield:xyz'})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn('error', body)
        self.assertIn('badfield', body['error'])

    def test_post_no_matches_returns_empty_groups(self):
        """Query that matches no owned cards returns total=0, groups=[]."""
        resp = self._post({'q': 'c:b'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['total_cards'], 0)
        self.assertEqual(data['groups'], [])

    def test_post_groups_sorted_box_then_divider(self):
        """Groups are sorted: box_name ASC, divider position ASC,
        Unassigned last."""
        div2 = collection_db.add_divider(self.conn, self.box, "D2", 2)
        box_b = collection_db.add_box(self.conn, "AAA Box")
        div_b = collection_db.add_divider(self.conn, box_b, "B1", 1)
        _seed(self.conn, "In D2", colors="R", divider_id=div2)
        _seed(self.conn, "In AAA", colors="R", divider_id=div_b)
        # A red card with no box or divider to anchor the Unassigned bucket.
        _seed(self.conn, "Red Orphan", colors="R")

        resp = self._post({'q': 'c:r'})
        self.assertEqual(resp.status_code, 200)
        groups = resp.get_json()['groups']
        box_names = [g['box_name'] for g in groups]
        # "AAA Box" < "Post Box" alphabetically; Unassigned last.
        self.assertEqual(box_names[0], 'AAA Box')
        self.assertEqual(box_names[-1], collection_db.UNASSIGNED_BOX_NAME)


if __name__ == "__main__":
    unittest.main()
