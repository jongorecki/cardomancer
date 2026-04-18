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


if __name__ == "__main__":
    unittest.main()
