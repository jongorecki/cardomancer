"""Unit tests for collection_db.py — SQLite collection database."""

import sys
import os
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collection_db


class TestCollectionDB(unittest.TestCase):

    def setUp(self):
        """Create a temporary database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_collection.db")
        self.conn = collection_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- Session management ---

    def test_start_session(self):
        sid = collection_db.start_session(self.conn, sort_mode="color")
        self.assertIsInstance(sid, int)
        self.assertGreater(sid, 0)

    def test_end_session(self):
        sid = collection_db.start_session(self.conn)
        collection_db.end_session(self.conn, sid, total_scans=5,
                                  recognized=4, unrecognized=1)
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        self.assertEqual(row['total_scans'], 5)
        self.assertEqual(row['recognized'], 4)
        self.assertIsNotNone(row['end_time'])

    def test_multiple_sessions(self):
        s1 = collection_db.start_session(self.conn, sort_mode="color")
        s2 = collection_db.start_session(self.conn, sort_mode="rarity")
        self.assertNotEqual(s1, s2)

    # --- Recording scans ---

    def test_record_recognized_scan(self):
        sid = collection_db.start_session(self.conn)
        card_info = {"Name": "Lightning Bolt", "Set": "m11",
                     "Colors": ["R"], "CMC": 1.0}
        card_data = {"collector_number": "149", "oracle_id": "bolt-oid",
                     "prices": {"usd": "1.50"}}
        collection_db.record_scan(self.conn, sid, scan_num=1,
                                  card_info=card_info, card_data=card_data,
                                  bin_num=1, method="hash", hash_distance=25.3)

        # Check scan_history
        scans = self.conn.execute(
            "SELECT * FROM scan_history WHERE session_id=?", (sid,)
        ).fetchall()
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0]['name'], "Lightning Bolt")
        self.assertEqual(scans[0]['recognized'], 1)

        # Check inventory
        inv = self.conn.execute(
            "SELECT * FROM inventory WHERE name=?", ("Lightning Bolt",)
        ).fetchall()
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv[0]['quantity'], 1)
        self.assertEqual(inv[0]['set_code'], "m11")

    def test_record_unrecognized_scan(self):
        sid = collection_db.start_session(self.conn)
        collection_db.record_scan(self.conn, sid, scan_num=1,
                                  card_info=None, bin_num=10)

        scans = self.conn.execute(
            "SELECT * FROM scan_history WHERE session_id=?", (sid,)
        ).fetchall()
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0]['recognized'], 0)

        # Inventory should be empty
        inv = self.conn.execute("SELECT * FROM inventory").fetchall()
        self.assertEqual(len(inv), 0)

    def test_duplicate_scan_increments_quantity(self):
        sid = collection_db.start_session(self.conn)
        card_info = {"Name": "Sol Ring", "Set": "c21", "Colors": [],
                     "CMC": 1.0}
        card_data = {"collector_number": "263", "oracle_id": "sol-oid",
                     "prices": {"usd": "2.00"}}

        collection_db.record_scan(self.conn, sid, 1, card_info=card_info,
                                  card_data=card_data, bin_num=1)
        collection_db.record_scan(self.conn, sid, 2, card_info=card_info,
                                  card_data=card_data, bin_num=1)
        collection_db.record_scan(self.conn, sid, 3, card_info=card_info,
                                  card_data=card_data, bin_num=1)

        # Scan history should have 3 entries
        scans = self.conn.execute(
            "SELECT * FROM scan_history WHERE session_id=?", (sid,)
        ).fetchall()
        self.assertEqual(len(scans), 3)

        # Inventory should have 1 entry with quantity 3
        inv = self.conn.execute(
            "SELECT * FROM inventory WHERE name=?", ("Sol Ring",)
        ).fetchall()
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv[0]['quantity'], 3)

    def test_record_scan_with_foil_flag(self):
        """A scan marked is_foil=True should populate the foil columns."""
        sid = collection_db.start_session(self.conn)
        info = {"Name": "Zombie Infestation", "Set": "ody", "Colors": ["B"],
                "CMC": 3.0}
        data = {"collector_number": "170", "prices": {"usd": "0.25"}}
        collection_db.record_scan(
            self.conn, sid, 1,
            card_info=info, card_data=data, bin_num=1,
            is_foil=True, foil_confidence=1.391,
        )

        row = self.conn.execute(
            "SELECT * FROM scan_history WHERE session_id=?", (sid,)
        ).fetchone()
        self.assertEqual(row['is_foil'], 1)
        self.assertAlmostEqual(row['foil_confidence'], 1.391, places=3)

        inv = self.conn.execute(
            "SELECT * FROM inventory WHERE name=?", ("Zombie Infestation",)
        ).fetchone()
        # Total quantity 1, foil_quantity 1
        self.assertEqual(inv['quantity'], 1)
        self.assertEqual(inv['foil_quantity'], 1)

    def test_record_scan_default_no_foil(self):
        """Scans that don't pass is_foil default to is_foil=0, foil_quantity=0."""
        sid = collection_db.start_session(self.conn)
        info = {"Name": "Lightning Bolt", "Set": "m11", "Colors": ["R"],
                "CMC": 1.0}
        data = {"collector_number": "149", "prices": {"usd": "1.50"}}
        collection_db.record_scan(self.conn, sid, 1,
                                  card_info=info, card_data=data, bin_num=1)
        row = self.conn.execute(
            "SELECT * FROM scan_history WHERE session_id=?", (sid,)
        ).fetchone()
        self.assertEqual(row['is_foil'], 0)
        self.assertIsNone(row['foil_confidence'])

        inv = self.conn.execute(
            "SELECT * FROM inventory WHERE name=?", ("Lightning Bolt",)
        ).fetchone()
        self.assertEqual(inv['quantity'], 1)
        self.assertEqual(inv['foil_quantity'], 0)

    def test_mixed_foil_nonfoil_inventory(self):
        """Scanning same card as nonfoil then foil increments both counters."""
        sid = collection_db.start_session(self.conn)
        info = {"Name": "Sol Ring", "Set": "c21", "Colors": [], "CMC": 1.0}
        data = {"collector_number": "263", "prices": {"usd": "2.00"}}

        # 2 nonfoils + 1 foil
        collection_db.record_scan(self.conn, sid, 1, card_info=info,
                                  card_data=data, bin_num=1, is_foil=False)
        collection_db.record_scan(self.conn, sid, 2, card_info=info,
                                  card_data=data, bin_num=1, is_foil=True,
                                  foil_confidence=2.1)
        collection_db.record_scan(self.conn, sid, 3, card_info=info,
                                  card_data=data, bin_num=1, is_foil=False)

        inv = self.conn.execute(
            "SELECT * FROM inventory WHERE name=?", ("Sol Ring",)
        ).fetchone()
        self.assertEqual(inv['quantity'], 3)
        self.assertEqual(inv['foil_quantity'], 1)

    def test_different_printings_separate_inventory(self):
        sid = collection_db.start_session(self.conn)
        info1 = {"Name": "Sol Ring", "Set": "c21", "Colors": [], "CMC": 1.0}
        data1 = {"collector_number": "263", "prices": {}}
        info2 = {"Name": "Sol Ring", "Set": "c20", "Colors": [], "CMC": 1.0}
        data2 = {"collector_number": "217", "prices": {}}

        collection_db.record_scan(self.conn, sid, 1, card_info=info1,
                                  card_data=data1, bin_num=1)
        collection_db.record_scan(self.conn, sid, 2, card_info=info2,
                                  card_data=data2, bin_num=1)

        inv = self.conn.execute(
            "SELECT * FROM inventory WHERE name=?", ("Sol Ring",)
        ).fetchall()
        self.assertEqual(len(inv), 2)

    # --- Query functions ---

    def test_get_collection_stats_empty(self):
        stats = collection_db.get_collection_stats(self.conn)
        self.assertEqual(stats['unique_cards'], 0)
        self.assertEqual(stats['total_cards'], 0)
        self.assertEqual(stats['total_value'], 0)

    def test_get_collection_stats_with_data(self):
        sid = collection_db.start_session(self.conn)
        info = {"Name": "Lightning Bolt", "Set": "m11", "Colors": ["R"],
                "CMC": 1.0}
        data = {"collector_number": "149", "prices": {"usd": "1.50"}}
        collection_db.record_scan(self.conn, sid, 1, card_info=info,
                                  card_data=data, bin_num=1)
        collection_db.record_scan(self.conn, sid, 2, card_info=info,
                                  card_data=data, bin_num=1)

        stats = collection_db.get_collection_stats(self.conn)
        self.assertEqual(stats['unique_cards'], 1)
        self.assertEqual(stats['total_cards'], 2)
        self.assertAlmostEqual(stats['total_value'], 3.0, places=1)

    def test_search_collection(self):
        sid = collection_db.start_session(self.conn)
        info1 = {"Name": "Lightning Bolt", "Set": "m11", "Colors": ["R"],
                 "CMC": 1.0, "Rarity": "common"}
        data1 = {"collector_number": "149", "rarity": "common",
                 "prices": {"usd": "1.50"}}
        info2 = {"Name": "Sol Ring", "Set": "c21", "Colors": [],
                 "CMC": 1.0}
        data2 = {"collector_number": "263", "rarity": "uncommon",
                 "prices": {"usd": "2.00"}}

        collection_db.record_scan(self.conn, sid, 1, card_info=info1,
                                  card_data=data1, bin_num=1)
        collection_db.record_scan(self.conn, sid, 2, card_info=info2,
                                  card_data=data2, bin_num=2)

        # Search by name
        results = collection_db.search_collection(self.conn, name="bolt")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], "Lightning Bolt")

        # Search by set
        results = collection_db.search_collection(self.conn, set_code="c21")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], "Sol Ring")

        # Search by price range
        results = collection_db.search_collection(self.conn, min_price=1.5)
        self.assertEqual(len(results), 2)

    def test_get_duplicates(self):
        sid = collection_db.start_session(self.conn)
        info = {"Name": "Sol Ring", "Set": "c21", "Colors": [], "CMC": 1.0}
        data = {"collector_number": "263", "prices": {"usd": "2.00"}}

        for i in range(4):
            collection_db.record_scan(self.conn, sid, i + 1, card_info=info,
                                      card_data=data, bin_num=1)

        dupes = collection_db.get_duplicates(self.conn, min_quantity=2)
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0]['quantity'], 4)

    def test_get_session_history(self):
        collection_db.start_session(self.conn, sort_mode="color")
        collection_db.start_session(self.conn, sort_mode="rarity")

        history = collection_db.get_session_history(self.conn)
        self.assertEqual(len(history), 2)

    def test_export_inventory_csv(self):
        sid = collection_db.start_session(self.conn)
        info = {"Name": "Forest", "Set": "m21", "Colors": [], "CMC": 0.0}
        data = {"collector_number": "313", "prices": {"usd": "0.05"}}
        collection_db.record_scan(self.conn, sid, 1, card_info=info,
                                  card_data=data, bin_num=1)

        csv_path = os.path.join(self.tmpdir, "export.csv")
        collection_db.export_inventory_csv(self.conn, csv_path)
        self.assertTrue(os.path.exists(csv_path))

        with open(csv_path, 'r') as f:
            content = f.read()
        self.assertIn("Forest", content)

    def test_card_info_price_fallback_when_scryfall_prices_null(self):
        # Regression: `or 0` used to coerce missing Scryfall prices to 0.0,
        # defeating the card_info['Price'] fallback at the bottom of record_scan.
        sid = collection_db.start_session(self.conn)
        info = {"Name": "Tarmogoyf", "Set": "fut", "Colors": ["G"],
                "CMC": 2.0, "Price": "$45.00"}
        data = {"collector_number": "153", "oracle_id": "goyf-oid",
                "prices": {"usd": None, "usd_foil": None}}
        collection_db.record_scan(self.conn, sid, 1, card_info=info,
                                  card_data=data, bin_num=1)
        row = self.conn.execute(
            "SELECT price_usd FROM inventory WHERE name=?",
            ("Tarmogoyf",)
        ).fetchone()
        self.assertAlmostEqual(row['price_usd'], 45.00, places=2)

    def test_reset_collection_clears_all_tables(self):
        # Regression: reset_collection used to leave wishlist, moxfield_*,
        # and detection_reviews populated.
        sid = collection_db.start_session(self.conn)
        info = {"Name": "Forest", "Set": "m21", "Colors": [], "CMC": 0.0}
        collection_db.record_scan(self.conn, sid, 1, card_info=info,
                                  card_data={}, bin_num=1)
        self.conn.execute(
            "INSERT INTO wishlist (name, added_date) VALUES (?, ?)",
            ("Black Lotus", "2026-01-01")
        )
        self.conn.execute(
            """INSERT INTO moxfield_wishlists
                   (source_key, username, last_synced, card_count)
               VALUES (?, ?, ?, ?)""",
            ("user/deck1", "tester", "2026-01-01", 0)
        )
        wid = self.conn.execute(
            "SELECT id FROM moxfield_wishlists WHERE source_key=?",
            ("user/deck1",)
        ).fetchone()['id']
        self.conn.execute(
            """INSERT INTO moxfield_wishlist_cards
                   (wishlist_id, oracle_id, name)
               VALUES (?, ?, ?)""",
            (wid, "oid-2", "Mox Ruby")
        )
        scan_id = self.conn.execute(
            "SELECT id FROM scan_history LIMIT 1"
        ).fetchone()['id']
        self.conn.execute(
            """INSERT INTO detection_reviews (scan_id, variable, detected_value)
               VALUES (?, ?, ?)""",
            (scan_id, "foil", None)
        )
        self.conn.commit()

        collection_db.reset_collection(self.conn)

        for table in ("inventory", "scan_history", "sessions", "boxes",
                      "dividers", "wishlist", "moxfield_wishlists",
                      "moxfield_wishlist_cards", "detection_reviews"):
            count = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()['n']
            self.assertEqual(count, 0, f"{table} not cleared by reset_collection")

    def test_seed_detection_reviews_idempotent(self):
        # Regression-cum-perf: seed used SELECT-then-INSERT; now uses
        # INSERT OR IGNORE relying on UNIQUE(scan_id, variable). Re-running
        # must not duplicate, and the returned count must reflect new rows only.
        sid = collection_db.start_session(self.conn)
        info = {"Name": "Forest", "Set": "m21", "Colors": [], "CMC": 0.0}
        collection_db.record_scan(self.conn, sid, 1, card_info=info,
                                  card_data={}, bin_num=1)

        first = collection_db.seed_detection_reviews_from_scans(self.conn)
        self.assertGreater(first, 0)

        second = collection_db.seed_detection_reviews_from_scans(self.conn)
        self.assertEqual(second, 0)

        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM detection_reviews"
        ).fetchone()['n']
        self.assertEqual(total, first)


class TestGetCullCandidates(unittest.TestCase):
    """Tests for get_cull_candidates() cross-DB query."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.col_path = os.path.join(self.tmpdir, "collection.db")
        self.enr_path = os.path.join(self.tmpdir, "enrichment.db")
        self.conn = collection_db.get_connection(db_path=self.col_path)

        # Build enrichment.db with full schema (needed by the new cull query
        # which references salt_scores, buylists, commander_ranks, deck_usage).
        import enrichment_db
        self._enr_conn = enrichment_db.get_connection(db_path=self.enr_path)

    def tearDown(self):
        self.conn.close()
        self._enr_conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add_inventory(self, name, oracle_id, price=0.25, qty=1,
                       type_line="Creature"):
        self.conn.execute(
            "INSERT INTO inventory (name, set_code, collector_number, oracle_id, "
            "type_line, rarity, colors, price_usd, quantity, first_scanned, last_scanned) "
            "VALUES (?, 'TST', '001', ?, ?, 'common', 'W', ?, ?, datetime('now'), datetime('now'))",
            (name, oracle_id, type_line, price, qty),
        )
        self.conn.commit()

    def _add_tag(self, oracle_id, tag_name):
        self._enr_conn.execute(
            "INSERT OR IGNORE INTO tags (oracle_id, tag_name, source) "
            "VALUES (?, ?, 'scryfall_search')",
            (oracle_id, tag_name),
        )
        self._enr_conn.commit()

    def _add_staple(self, oracle_id):
        self._enr_conn.execute(
            "INSERT OR IGNORE INTO staples "
            "(oracle_id, tier, source, score, archetypes_json, last_updated) "
            "VALUES (?, 'universal', 'edhrec', 0.5, NULL, '2026-01-01')",
            (oracle_id,),
        )
        self._enr_conn.commit()

    def test_vanilla_below_threshold_returned(self):
        self._add_inventory("Grizzly Bears", "oid-bears", price=0.15)
        self._add_tag("oid-bears", "vanilla")
        result = collection_db.get_cull_candidates(
            self.conn, max_price=1.0, enr_db_path=self.enr_path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Grizzly Bears")
        self.assertIn("vanilla", result[0]["cull_reasons"])
        self.assertIn("no_staple", result[0]["cull_reasons"])

    def test_french_vanilla_included(self):
        self._add_inventory("Squire", "oid-squire", price=0.10)
        self._add_tag("oid-squire", "french-vanilla")
        result = collection_db.get_cull_candidates(
            self.conn, max_price=1.0, enr_db_path=self.enr_path)
        self.assertEqual(len(result), 1)
        self.assertIn("french-vanilla", result[0]["cull_reasons"])

    def test_staple_excluded(self):
        self._add_inventory("Sol Ring", "oid-sol-ring", price=0.50)
        self._add_tag("oid-sol-ring", "vanilla")   # hypothetically tagged
        self._add_staple("oid-sol-ring")
        result = collection_db.get_cull_candidates(
            self.conn, max_price=1.0, enr_db_path=self.enr_path)
        self.assertEqual(len(result), 0, "Staple cards must be excluded")

    def test_above_price_threshold_excluded(self):
        self._add_inventory("Pricey Vanilla", "oid-pricey", price=2.50)
        self._add_tag("oid-pricey", "vanilla")
        result = collection_db.get_cull_candidates(
            self.conn, max_price=1.0, enr_db_path=self.enr_path)
        self.assertEqual(len(result), 0, "Cards above price threshold must be excluded")

    def test_custom_price_threshold(self):
        self._add_inventory("Cheap Vanilla", "oid-cheap", price=0.05)
        self._add_inventory("Mid Vanilla", "oid-mid", price=0.75)
        self._add_tag("oid-cheap", "vanilla")
        self._add_tag("oid-mid", "vanilla")
        result = collection_db.get_cull_candidates(
            self.conn, max_price=0.50, enr_db_path=self.enr_path)
        oids = {r["oracle_id"] for r in result}
        self.assertIn("oid-cheap", oids)
        self.assertNotIn("oid-mid", oids)

    def test_no_tag_not_returned(self):
        self._add_inventory("Lightning Bolt", "oid-bolt", price=0.30)
        # No tag added
        result = collection_db.get_cull_candidates(
            self.conn, max_price=1.0, enr_db_path=self.enr_path)
        self.assertEqual(len(result), 0)

    def test_missing_enrichment_db_returns_empty(self):
        result = collection_db.get_cull_candidates(
            self.conn, max_price=1.0,
            enr_db_path=os.path.join(self.tmpdir, "nonexistent.db"))
        self.assertEqual(result, [])

    def test_null_price_included(self):
        """Cards with no price data are included (treated as worthless)."""
        self._add_inventory("No-Price Vanilla", "oid-noprice", price=None)
        self._add_tag("oid-noprice", "vanilla")
        result = collection_db.get_cull_candidates(
            self.conn, max_price=1.0, enr_db_path=self.enr_path)
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
