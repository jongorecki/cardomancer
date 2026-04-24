# tests/enrichment/test_tagger.py

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class TestTaggerIdempotency(unittest.TestCase):
    """Running refresh() twice must produce identical DB state."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _count(self, table: str) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_double_refresh_same_tag_count(self):
        """Two full refreshes against same mocked data → same row count."""
        source = TaggerSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            for _ in range(2):
                with patch.object(source, "_scryfall_reachable",
                                  return_value=True):
                    with patch.object(source, "probe", return_value=True):
                        with patch.object(source, "_fetch_tag",
                                          return_value=(["oid-x", "oid-y"], 2)):
                            with patch.object(source, "_fetch_atag",
                                              return_value=([], None)):
                                source.refresh(full=True)
        finally:
            enrichment_db.DB_PATH = orig

        # The UPSERT logic should keep exactly one row per (oracle_id, tag_name)
        count = self._count("tags")
        # Each tag in KNOWN_FUNCTION_TAGS got 2 oracle_ids → could be many,
        # but count is stable between runs.
        self.assertGreater(count, 0)

        # Run once more and confirm count doesn't change
        orig2 = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "_scryfall_reachable", return_value=True):
                with patch.object(source, "probe", return_value=True):
                    with patch.object(source, "_fetch_tag",
                                      return_value=(["oid-x", "oid-y"], 2)):
                        with patch.object(source, "_fetch_atag",
                                          return_value=([], None)):
                            source.refresh(full=True)
        finally:
            enrichment_db.DB_PATH = orig2

        self.assertEqual(count, self._count("tags"),
                         "Row count must not change after idempotent re-refresh")


class TestFetchTagRateLimit(unittest.TestCase):
    """_fetch_tag must handle 429 with backoff and return gracefully."""

    def test_fetch_tag_429_retries_then_aborts(self):
        """Simulate 3 429s in a row — _fetch_tag backs off and returns empty."""
        import httpx as _httpx

        call_count = [0]

        class FakeClient:
            def get(self, url, **kw):
                call_count[0] += 1
                r = MagicMock()
                r.status_code = 429
                r.is_success = False
                r.json.return_value = {}
                return r

        warnings: list[str] = []
        with patch("time.sleep"):  # skip actual sleep
            result_ids, total = TaggerSource._fetch_tag(
                FakeClient(), "otag:removal", warnings)

        self.assertEqual(result_ids, [])
        self.assertIsNone(total)
        self.assertTrue(
            any("retries exceeded" in w or "Rate-limit" in w or
                "rate-limit" in w.lower() for w in warnings),
            f"Expected rate-limit warning, got: {warnings}"
        )

    def test_fetch_tag_recovers_after_single_429(self):
        """A single 429 followed by a 200 should succeed."""
        import httpx as _httpx

        attempts = [0]

        def fake_get(url, **kw):
            attempts[0] += 1
            r = MagicMock()
            if attempts[0] == 1:
                r.status_code = 429
                r.is_success = False
                r.json.return_value = {}
            else:
                r.status_code = 200
                r.is_success = True
                r.json.return_value = {
                    "total_cards": 2,
                    "data": [
                        {"oracle_id": "oid-a"},
                        {"oracle_id": "oid-b"},
                    ],
                    "has_more": False,
                }
            return r

        class FakeClient:
            def get(self, url, **kw):
                return fake_get(url, **kw)

        warnings: list[str] = []
        with patch("time.sleep"):
            result_ids, total = TaggerSource._fetch_tag(
                FakeClient(), "otag:removal", warnings)

        self.assertEqual(sorted(result_ids), ["oid-a", "oid-b"])
        self.assertEqual(total, 2)


class TestFetchTagPagination(unittest.TestCase):
    """_fetch_tag must follow has_more / next_page pagination."""

    def test_paginated_results_collected(self):
        page2_url = "https://api.scryfall.com/cards/search?q=otag%3Aramp&page=2"

        pages = [
            {
                "total_cards": 4,
                "data": [{"oracle_id": "oid-1"}, {"oracle_id": "oid-2"}],
                "has_more": True,
                "next_page": page2_url,
            },
            {
                "total_cards": 4,
                "data": [{"oracle_id": "oid-3"}, {"oracle_id": "oid-4"}],
                "has_more": False,
            },
        ]
        call_n = [0]

        class FakeClient:
            def get(self, url, **kw):
                idx = call_n[0]
                call_n[0] += 1
                r = MagicMock()
                r.status_code = 200
                r.is_success = True
                r.json.return_value = pages[min(idx, len(pages) - 1)]
                return r

        warnings: list[str] = []
        with patch("time.sleep"):
            result_ids, total = TaggerSource._fetch_tag(
                FakeClient(), "otag:ramp", warnings)

        self.assertEqual(sorted(result_ids), ["oid-1", "oid-2", "oid-3", "oid-4"])
        self.assertEqual(total, 4)


if __name__ == "__main__":
    unittest.main()
