"""Unit tests for Phase 2.12 storage tables: boxes + dividers CRUD and
the inventory.divider_id relationship."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collection_db


class TestBoxesCRUD(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_collection.db")
        self.conn = collection_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_box_assigns_id_and_persists(self):
        bid = collection_db.add_box(self.conn, "Storage A",
                                    capacity=500, notes="shelf 1")
        self.assertIsInstance(bid, int)
        row = self.conn.execute(
            "SELECT * FROM boxes WHERE id=?", (bid,)
        ).fetchone()
        self.assertEqual(row['name'], "Storage A")
        self.assertEqual(row['capacity'], 500)
        self.assertEqual(row['notes'], "shelf 1")
        self.assertIsNotNone(row['created_at'])

    def test_box_name_unique(self):
        collection_db.add_box(self.conn, "Dup")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            collection_db.add_box(self.conn, "Dup")

    def test_update_box_partial(self):
        bid = collection_db.add_box(self.conn, "Old", capacity=100)
        collection_db.update_box(self.conn, bid, name="New")
        row = self.conn.execute(
            "SELECT * FROM boxes WHERE id=?", (bid,)
        ).fetchone()
        self.assertEqual(row['name'], "New")
        self.assertEqual(row['capacity'], 100)  # untouched

    def test_update_nonexistent_box_returns_false(self):
        self.assertFalse(collection_db.update_box(self.conn, 9999, name="x"))

    def test_list_boxes_includes_counts(self):
        b1 = collection_db.add_box(self.conn, "Aaa")
        b2 = collection_db.add_box(self.conn, "Bbb")
        d1 = collection_db.add_divider(self.conn, b1, "tab1", 1)

        # Seed one inventory row and attach it to divider d1.
        item_id = collection_db.add_inventory_item(
            self.conn, name="Lightning Bolt", set_code="m11",
            quantity=3,
        )
        collection_db.assign_inventory_divider(self.conn, item_id, d1)

        boxes = collection_db.list_boxes(self.conn)
        self.assertEqual([b['name'] for b in boxes], ["Aaa", "Bbb"])
        by_id = {b['id']: b for b in boxes}
        self.assertEqual(by_id[b1]['divider_count'], 1)
        self.assertEqual(by_id[b1]['card_count'], 3)
        self.assertEqual(by_id[b2]['divider_count'], 0)
        self.assertEqual(by_id[b2]['card_count'], 0)

    def test_delete_box_cascades_dividers_and_unassigns_inventory(self):
        bid = collection_db.add_box(self.conn, "Trash")
        did = collection_db.add_divider(self.conn, bid, "t", 1)
        item_id = collection_db.add_inventory_item(
            self.conn, name="Island", set_code="unf", quantity=1
        )
        collection_db.assign_inventory_divider(self.conn, item_id, did)

        self.assertTrue(collection_db.delete_box(self.conn, bid))

        # Divider gone.
        self.assertIsNone(
            self.conn.execute(
                "SELECT id FROM dividers WHERE id=?", (did,)
            ).fetchone()
        )
        # Inventory row survives with divider_id cleared.
        row = self.conn.execute(
            "SELECT divider_id FROM inventory WHERE id=?", (item_id,)
        ).fetchone()
        self.assertIsNone(row['divider_id'])


class TestDividersCRUD(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_collection.db")
        self.conn = collection_db.get_connection(db_path=self.db_path)
        self.box_id = collection_db.add_box(self.conn, "BoxA", capacity=100)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_divider_rejects_missing_box(self):
        with self.assertRaises(ValueError):
            collection_db.add_divider(self.conn, 9999, "x", 1)

    def test_add_divider_persists(self):
        did = collection_db.add_divider(
            self.conn, self.box_id, "Red", 3, capacity=20
        )
        row = self.conn.execute(
            "SELECT * FROM dividers WHERE id=?", (did,)
        ).fetchone()
        self.assertEqual(row['label'], "Red")
        self.assertEqual(row['position'], 3)
        self.assertEqual(row['capacity'], 20)
        self.assertEqual(row['box_id'], self.box_id)

    def test_list_dividers_scoped_by_box_and_sorted(self):
        collection_db.add_divider(self.conn, self.box_id, "b", 2)
        collection_db.add_divider(self.conn, self.box_id, "a", 1)
        other = collection_db.add_box(self.conn, "BoxB")
        collection_db.add_divider(self.conn, other, "x", 5)

        dividers = collection_db.list_dividers(self.conn, box_id=self.box_id)
        self.assertEqual([d['label'] for d in dividers], ["a", "b"])
        self.assertEqual(dividers[0]['box_name'], "BoxA")
        self.assertNotIn("x", [d['label'] for d in dividers])

        all_ = collection_db.list_dividers(self.conn)
        self.assertEqual(len(all_), 3)

    def test_update_divider_partial(self):
        did = collection_db.add_divider(self.conn, self.box_id, "a", 1)
        collection_db.update_divider(self.conn, did, label="renamed",
                                     position=7)
        row = self.conn.execute(
            "SELECT * FROM dividers WHERE id=?", (did,)
        ).fetchone()
        self.assertEqual(row['label'], "renamed")
        self.assertEqual(row['position'], 7)

    def test_delete_divider_unassigns_inventory(self):
        did = collection_db.add_divider(self.conn, self.box_id, "a", 1)
        item = collection_db.add_inventory_item(
            self.conn, name="Swamp", set_code="unf", quantity=1
        )
        collection_db.assign_inventory_divider(self.conn, item, did)
        self.assertTrue(collection_db.delete_divider(self.conn, did))
        row = self.conn.execute(
            "SELECT divider_id FROM inventory WHERE id=?", (item,)
        ).fetchone()
        self.assertIsNone(row['divider_id'])


class TestInventoryDividerAssignment(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_collection.db")
        self.conn = collection_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_assign_inventory_divider_rejects_missing(self):
        item = collection_db.add_inventory_item(
            self.conn, name="X", set_code="a", quantity=1
        )
        with self.assertRaises(ValueError):
            collection_db.assign_inventory_divider(self.conn, item, 9999)

    def test_assign_and_clear(self):
        box = collection_db.add_box(self.conn, "B")
        div = collection_db.add_divider(self.conn, box, "t", 1)
        item = collection_db.add_inventory_item(
            self.conn, name="X", set_code="a", quantity=1
        )
        self.assertTrue(
            collection_db.assign_inventory_divider(self.conn, item, div)
        )
        row = self.conn.execute(
            "SELECT divider_id FROM inventory WHERE id=?", (item,)
        ).fetchone()
        self.assertEqual(row['divider_id'], div)

        # Clear.
        collection_db.assign_inventory_divider(self.conn, item, None)
        row = self.conn.execute(
            "SELECT divider_id FROM inventory WHERE id=?", (item,)
        ).fetchone()
        self.assertIsNone(row['divider_id'])

    def test_assign_nonexistent_item(self):
        box = collection_db.add_box(self.conn, "B")
        div = collection_db.add_divider(self.conn, box, "t", 1)
        self.assertFalse(
            collection_db.assign_inventory_divider(self.conn, 9999, div)
        )


if __name__ == "__main__":
    unittest.main()
