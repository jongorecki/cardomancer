# tests/enrichment/test_tagger.py

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import enrichment_db
from web_enrichment.tagger import TaggerSource, KNOWN_FUNCTION_TAGS, KNOWN_ART_TAGS


class TestTaggerKnownTags(unittest.TestCase):

    def test_no_duplicate_function_tags(self):
        self.assertEqual(len(KNOWN_FUNCTION_TAGS), len(set(KNOWN_FUNCTION_TAGS)))

    def test_no_duplicate_art_tags(self):
        self.assertEqual(len(KNOWN_ART_TAGS), len(set(KNOWN_ART_TAGS)))

    def test_contains_removal(self):
        self.assertIn("removal", KNOWN_FUNCTION_TAGS)

    def test_contains_vanilla(self):
        self.assertIn("vanilla", KNOWN_FUNCTION_TAGS)

    def test_contains_ramp(self):
        self.assertIn("ramp", KNOWN_FUNCTION_TAGS)

    def test_art_tags_non_empty(self):
        self.assertGreater(len(KNOWN_ART_TAGS), 0)


class TestTaggerSourceWrite(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _query(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _conn(self):
        return enrichment_db.get_connection(db_path=self.db_path)

    def test_write_tag_rows(self):
        tag_rows = [
            {"oracle_id": "oid-a", "tag_name": "removal",
             "source": "scryfall_search"},
            {"oracle_id": "oid-b", "tag_name": "removal",
             "source": "scryfall_search"},
        ]
        conn = self._conn()
        TaggerSource._write(conn, tag_rows, [], [], [])
        conn.close()

        rows = self._query("SELECT * FROM tags WHERE tag_name='removal'")
        self.assertEqual(len(rows), 2)

    def test_write_deduplicates(self):
        tag_rows = [
            {"oracle_id": "oid-a", "tag_name": "removal",
             "source": "scryfall_search"},
            {"oracle_id": "oid-a", "tag_name": "removal",
             "source": "scryfall_search"},
        ]
        conn = self._conn()
        TaggerSource._write(conn, tag_rows, [], [], [])
        conn.close()

        rows = self._query("SELECT COUNT(*) as cnt FROM tags WHERE oracle_id='oid-a'")
        self.assertEqual(rows[0]["cnt"], 1)

    def test_write_catalog(self):
        catalog = [{"tag_name": "draw", "tag_type": "function", "parent": None,
                    "description": None, "card_count_expected": 500,
                    "source": "fallback"}]
        conn = self._conn()
        TaggerSource._write(conn, [], [], catalog, [])
        conn.close()

        rows = self._query("SELECT * FROM tag_catalog WHERE tag_name='draw'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["card_count_expected"], 500)

    def test_existing_tag_preserved_on_empty_refresh(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO tags (oracle_id, tag_name, source) "
            "VALUES ('existing-oid', 'removal', 'scryfall_search')"
        )
        conn.commit()
        conn.close()

        source = TaggerSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "_scryfall_reachable", return_value=True):
                with patch.object(source, "probe", return_value=True):
                    with patch.object(source, "_fetch_tag",
                                      return_value=([], None)):
                        with patch.object(source, "_fetch_atag",
                                          return_value=([], None)):
                            source.refresh(full=True)
        finally:
            enrichment_db.DB_PATH = orig

        rows = self._query(
            "SELECT * FROM tags WHERE oracle_id='existing-oid'")
        self.assertGreater(len(rows), 0,
                           "Existing tag row must survive empty refresh")


class TestTaggerRefreshIntegration(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _query(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_refresh_writes_tag_rows_on_results(self):
        source = TaggerSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "_scryfall_reachable", return_value=True):
                with patch.object(source, "probe", return_value=True):
                    with patch.object(source, "_fetch_tag",
                                      return_value=(["oracle-abc", "oracle-def"], 2)):
                        with patch.object(source, "_fetch_atag",
                                          return_value=([], None)):
                            result = source.refresh(full=True)
        finally:
            enrichment_db.DB_PATH = orig

        self.assertTrue(result.success)
        rows = self._query("SELECT COUNT(*) as cnt FROM tags")
        self.assertGreater(rows[0]["cnt"], 0)

    def test_scryfall_unreachable_aborts(self):
        source = TaggerSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "_scryfall_reachable", return_value=False):
                result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig
        self.assertFalse(result.success)

    def test_empty_tag_response_no_crash(self):
        source = TaggerSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "_scryfall_reachable", return_value=True):
                with patch.object(source, "probe", return_value=True):
                    with patch.object(source, "_fetch_tag",
                                      return_value=([], None)):
                        with patch.object(source, "_fetch_atag",
                                          return_value=([], None)):
                            result = source.refresh(full=True)
        finally:
            enrichment_db.DB_PATH = orig
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
