# tests/enrichment/test_spellbook.py

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
from web_enrichment.spellbook import SpellbookSource


class TestSpellbookSourceParsing(unittest.TestCase):

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

    def _run_refresh(self, combos, members):
        source = SpellbookSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "_fetch_all", return_value=(combos, members)):
                with patch.object(source, "probe", return_value=True):
                    return source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

    def test_happy_path_writes_combos(self):
        combos = [{
            "combo_id": "abc", "result": "Win the game",
            "identity": "UB", "mana_needed": "{1}",
            "prerequisites_json": "[]", "source": "spellbook",
        }]
        members = [
            {"oracle_id": "oid-1", "combo_id": "abc", "quantity": 1},
            {"oracle_id": "oid-2", "combo_id": "abc", "quantity": 1},
        ]
        result = self._run_refresh(combos, members)
        self.assertTrue(result.success)
        self.assertGreater(result.rows_changed, 0)

        rows = self._query("SELECT * FROM combos WHERE combo_id='abc'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result"], "Win the game")

        members_rows = self._query(
            "SELECT oracle_id FROM combo_membership WHERE combo_id='abc'")
        self.assertEqual(len(members_rows), 2)

    def test_idempotent_refresh(self):
        combos = [{
            "combo_id": "xyz", "result": "Infinite mana",
            "identity": "C", "mana_needed": None,
            "prerequisites_json": "[]", "source": "spellbook",
        }]
        members = [{"oracle_id": "oid-a", "combo_id": "xyz", "quantity": 1}]

        self._run_refresh(combos, members)
        result2 = self._run_refresh(combos, members)
        self.assertTrue(result2.success)

        rows = self._query("SELECT COUNT(*) as cnt FROM combos WHERE combo_id='xyz'")
        self.assertEqual(rows[0]["cnt"], 1)

    def test_empty_response_preserves_existing(self):
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.execute(
            "INSERT INTO combos (combo_id, result, identity, mana_needed, "
            "prerequisites_json, source) VALUES ('exist','X','W',null,'[]','spellbook')"
        )
        conn.commit()
        conn.close()

        result = self._run_refresh([], [])
        self.assertTrue(result.success)

        rows = self._query("SELECT * FROM combos WHERE combo_id='exist'")
        self.assertGreater(len(rows), 0, "Existing combo must survive empty refresh")

    def test_probe_failure_aborts(self):
        source = SpellbookSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=False):
                result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig
        self.assertFalse(result.success)
        self.assertTrue(any("probe" in e.lower() for e in result.errors))


class TestSpellbookFixture(unittest.TestCase):

    def test_fixture_has_expected_keys(self):
        fpath = (Path(__file__).parent.parent /
                 "fixtures" / "spellbook" / "variants_page1.json")
        if not fpath.exists():
            self.skipTest("Spellbook fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        self.assertIn("results", data)
        if data["results"]:
            r0 = data["results"][0]
            self.assertIn("id", r0)
            self.assertIn("uses", r0)
            self.assertIn("produces", r0)
            self.assertIn("identity", r0)

    def test_uses_has_oracle_id(self):
        fpath = (Path(__file__).parent.parent /
                 "fixtures" / "spellbook" / "variants_page1.json")
        if not fpath.exists():
            self.skipTest("Spellbook fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        for combo in data.get("results", []):
            for use in combo.get("uses", []):
                card = use.get("card", {})
                self.assertIn("oracleId", card,
                              f"combo {combo['id']} use missing oracleId")


if __name__ == "__main__":
    unittest.main()
