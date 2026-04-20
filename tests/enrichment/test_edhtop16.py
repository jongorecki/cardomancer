# tests/enrichment/test_edhtop16.py

from __future__ import annotations

import json
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
from web_enrichment.edhtop16 import EDHTop16Source, _is_basic_land, CEDH_THRESHOLD


class TestBasicLandFilter(unittest.TestCase):

    def test_detects_basic_land(self):
        self.assertTrue(_is_basic_land("Basic Land \u2014 Forest"))
        self.assertTrue(_is_basic_land("Basic Land \u2014 Island"))
        self.assertTrue(_is_basic_land("Basic Snow Land \u2014 Mountain"))
        self.assertTrue(_is_basic_land("basic land"))

    def test_does_not_filter_normal_cards(self):
        self.assertFalse(_is_basic_land("Artifact"))
        self.assertFalse(_is_basic_land("Instant"))
        self.assertFalse(_is_basic_land("Legendary Creature \u2014 Human"))
        self.assertFalse(_is_basic_land("Land"))
        self.assertFalse(_is_basic_land(""))


class TestEDHTop16Source(unittest.TestCase):

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

    def _run_refresh(self, staples: list[dict]):
        source = EDHTop16Source()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(EDHTop16Source, "_fetch_staples",
                                  return_value=staples):
                    return source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

    def test_writes_cedh_staples(self):
        staples = [
            {"name": "Sol Ring",
             "oracleId": "6ad8011d-3471-4369-9d68-b264cc027487",
             "colorId": "C", "type": "Artifact",
             "playRateLastYear": 0.48},
            {"name": "Thassa's Oracle",
             "oracleId": "oracle-thassa",
             "colorId": "U", "type": "Creature",
             "playRateLastYear": 0.85},
        ]
        result = self._run_refresh(staples)
        self.assertTrue(result.success)

        rows = self._query(
            "SELECT oracle_id, tier FROM staples WHERE source='edhtop16'")
        oids = {r["oracle_id"] for r in rows}
        self.assertIn("6ad8011d-3471-4369-9d68-b264cc027487", oids)
        self.assertIn("oracle-thassa", oids)
        for r in rows:
            self.assertEqual(r["tier"], "cedh")

    def test_excludes_basic_lands(self):
        staples = [
            {"name": "Forest", "oracleId": "basic-forest",
             "colorId": "G", "type": "Basic Land \u2014 Forest",
             "playRateLastYear": 0.99},
            {"name": "Sol Ring", "oracleId": "sol-ring-oracle",
             "colorId": "C", "type": "Artifact",
             "playRateLastYear": 0.50},
        ]
        result = self._run_refresh(staples)
        self.assertTrue(result.success)

        basic = self._query(
            "SELECT * FROM staples WHERE oracle_id='basic-forest'")
        self.assertEqual(len(basic), 0, "Basic land must not be a staple")

        sol = self._query(
            "SELECT * FROM staples WHERE oracle_id='sol-ring-oracle'")
        self.assertGreater(len(sol), 0)

    def test_respects_threshold(self):
        staples = [
            {"name": "Low Play Card", "oracleId": "below-threshold",
             "colorId": "W", "type": "Enchantment",
             "playRateLastYear": 0.05},  # below 0.15
            {"name": "High Play Card", "oracleId": "above-threshold",
             "colorId": "U", "type": "Instant",
             "playRateLastYear": 0.20},
        ]
        result = self._run_refresh(staples)
        self.assertTrue(result.success)

        below = self._query(
            "SELECT * FROM staples WHERE oracle_id='below-threshold'")
        self.assertEqual(len(below), 0)

        above = self._query(
            "SELECT * FROM staples WHERE oracle_id='above-threshold'")
        self.assertGreater(len(above), 0)

    def test_idempotent(self):
        staples = [
            {"name": "Sol Ring", "oracleId": "sol-ring-oracle",
             "colorId": "C", "type": "Artifact",
             "playRateLastYear": 0.50},
        ]
        self._run_refresh(staples)
        result2 = self._run_refresh(staples)
        self.assertTrue(result2.success)

        rows = self._query(
            "SELECT COUNT(*) as cnt FROM staples WHERE oracle_id='sol-ring-oracle'")
        self.assertEqual(rows[0]["cnt"], 1)

    def test_probe_failure_aborts(self):
        source = EDHTop16Source()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=False):
                result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig
        self.assertFalse(result.success)

    def test_empty_response_noop(self):
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.execute(
            "INSERT INTO staples (oracle_id, tier, source, score, last_updated) "
            "VALUES ('existing', 'cedh', 'edhtop16', 0.5, '2026-01-01')"
        )
        conn.commit()
        conn.close()

        result = self._run_refresh([])
        self.assertTrue(result.success)

        rows = self._query(
            "SELECT * FROM staples WHERE oracle_id='existing'")
        self.assertGreater(len(rows), 0, "Existing row must survive empty refresh")


class TestEDHTop16Fixture(unittest.TestCase):

    def test_fixture_structure(self):
        fpath = (Path(__file__).parent.parent /
                 "fixtures" / "edhtop16" / "staples_response.json")
        if not fpath.exists():
            self.skipTest("edhtop16 fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        self.assertIn("data", data)
        staples = data["data"].get("staples", [])
        self.assertIsInstance(staples, list)
        if staples:
            s = staples[0]
            self.assertIn("oracleId", s)
            self.assertIn("playRateLastYear", s)


if __name__ == "__main__":
    unittest.main()
