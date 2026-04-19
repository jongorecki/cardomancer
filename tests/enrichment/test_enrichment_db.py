# tests/enrichment/test_enrichment_db.py
# ---------------------------------------------------------------------------
# Exercises the enrichment_db schema + helpers end-to-end.
# Matches existing test_collection_db.py style (unittest.TestCase,
# tempfile fixture, run via pytest).
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import enrichment_db


EXPECTED_TABLES = {
    "tags", "art_tags", "tag_catalog",
    "staples", "salt_scores", "themes",
    "combos", "combo_membership", "commander_ranks",
    "buylists", "price_history",
    "local_tags", "sync_metadata", "coverage_reports",
    "card_universe",
}


class TestEnrichmentDB(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="enrichment_test_")
        self.db_path = os.path.join(self.tmpdir, "enrichment.db")
        self.conn = enrichment_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_all_tables_created(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r["name"] for r in rows}
        missing = EXPECTED_TABLES - names
        self.assertFalse(missing, f"Missing tables: {missing}")

    def test_migrations_idempotent(self):
        # Re-running the migration must leave the DB in the same state.
        enrichment_db._create_tables(self.conn)
        enrichment_db._create_tables(self.conn)
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r["name"] for r in rows}
        self.assertTrue(EXPECTED_TABLES.issubset(names))

    def test_indexes_present(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        names = {r["name"] for r in rows}
        for idx in ("idx_tags_tag", "idx_art_tags_tag", "idx_staples_tier"):
            self.assertIn(idx, names)

    def test_insert_and_read_tag(self):
        self.conn.execute(
            "INSERT INTO tags (oracle_id, tag_name, source) VALUES (?, ?, ?)",
            ("oid-1", "removal", "scryfall_search"),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM tags WHERE oracle_id = ?", ("oid-1",)
        ).fetchone()
        self.assertEqual(row["tag_name"], "removal")
        self.assertEqual(row["source"], "scryfall_search")

    def test_seed_card_universe(self):
        entries = [("oid-a", "Card A"), ("oid-b", "Card B")]
        n = enrichment_db.seed_card_universe(self.conn, entries)
        self.assertEqual(n, 2)

        rows = self.conn.execute(
            "SELECT oracle_id, name FROM card_universe ORDER BY oracle_id"
        ).fetchall()
        self.assertEqual([(r["oracle_id"], r["name"]) for r in rows],
                         [("oid-a", "Card A"), ("oid-b", "Card B")])

        # Re-seeding updates existing rows idempotently.
        enrichment_db.seed_card_universe(
            self.conn, [("oid-a", "Card A v2")])
        row = self.conn.execute(
            "SELECT name FROM card_universe WHERE oracle_id = ?", ("oid-a",)
        ).fetchone()
        self.assertEqual(row["name"], "Card A v2")

    def test_record_sync_attempt_success_then_failure(self):
        enrichment_db.record_sync_attempt(
            self.conn, "tagger", success=True, coverage_pct=95.0)
        row = self.conn.execute(
            "SELECT * FROM sync_metadata WHERE source='tagger'"
        ).fetchone()
        self.assertIsNotNone(row["last_success"])
        self.assertIsNone(row["error"])
        self.assertAlmostEqual(row["coverage_pct"], 95.0)

        enrichment_db.record_sync_attempt(
            self.conn, "tagger", success=False, error="network timeout")
        row = self.conn.execute(
            "SELECT * FROM sync_metadata WHERE source='tagger'"
        ).fetchone()
        self.assertEqual(row["error"], "network timeout")
        # Prior last_success preserved on failure.
        self.assertIsNotNone(row["last_success"])

    def test_record_coverage_appends(self):
        enrichment_db.record_coverage(
            self.conn, "tagger", "removal", 500, 495)
        enrichment_db.record_coverage(
            self.conn, "tagger", "ramp", 200, 200)
        rows = self.conn.execute(
            "SELECT source, key_name, expected, actual "
            "FROM coverage_reports ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["key_name"], "removal")
        self.assertEqual(rows[1]["actual"], 200)


class TestCrossDbJoin(unittest.TestCase):
    """Sanity: the collection/enrichment join pattern (LEFT JOIN via
    ATTACH DATABASE) works against empty + seeded fixtures."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="xdb_test_")
        self.enrichment_path = os.path.join(self.tmpdir, "enrichment.db")
        self.collection_path = os.path.join(self.tmpdir, "collection.db")

        self.enrichment = enrichment_db.get_connection(
            db_path=self.enrichment_path)
        import collection_db
        self.collection = collection_db.get_connection(
            db_path=self.collection_path)

    def tearDown(self):
        self.enrichment.close()
        self.collection.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_left_join_empty_returns_zero(self):
        self.collection.execute("ATTACH DATABASE ? AS enr",
                                (self.enrichment_path,))
        row = self.collection.execute(
            "SELECT COUNT(*) AS c FROM inventory "
            "LEFT JOIN enr.tags t ON t.oracle_id = inventory.oracle_id"
        ).fetchone()
        self.assertEqual(row["c"], 0)

    def test_left_join_with_seed(self):
        # Seed one inventory card + one enrichment tag for that oracle_id.
        self.collection.execute(
            "INSERT INTO inventory "
            "(name, set_code, collector_number, oracle_id, quantity, "
            " first_scanned, last_scanned) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            ("Sol Ring", "cmr", "263", "oracle-sol-ring", 1),
        )
        self.collection.commit()

        self.enrichment.execute(
            "INSERT INTO tags (oracle_id, tag_name, source) "
            "VALUES (?, ?, ?)",
            ("oracle-sol-ring", "ramp", "scryfall_search"),
        )
        self.enrichment.commit()

        self.collection.execute("ATTACH DATABASE ? AS enr",
                                (self.enrichment_path,))
        rows = self.collection.execute(
            "SELECT i.name, t.tag_name FROM inventory i "
            "LEFT JOIN enr.tags t ON t.oracle_id = i.oracle_id "
            "WHERE i.oracle_id = 'oracle-sol-ring'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Sol Ring")
        self.assertEqual(rows[0]["tag_name"], "ramp")


if __name__ == "__main__":
    unittest.main()
