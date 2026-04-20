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
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import enrichment_db
from web_enrichment.edhtop16 import (
    EDHTop16Source, _is_basic_land, CEDH_THRESHOLD,
)


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

    def _run_refresh(self, card_counts: dict, total_entries: int):
        """Run refresh with _fetch_tournament_decks mocked to return (card_counts, total_entries)."""
        source = EDHTop16Source()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(
                    source, "_fetch_tournament_decks",
                    return_value=(card_counts, total_entries),
                ):
                    return source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

    def test_writes_cedh_staples(self):
        # 48/100 = 48%, 85/100 = 85% — both above 15% threshold
        result = self._run_refresh(
            card_counts={
                "6ad8011d-3471-4369-9d68-b264cc027487": 48,
                "oracle-thassa": 85,
            },
            total_entries=100,
        )
        self.assertTrue(result.success)

        rows = self._query(
            "SELECT oracle_id, tier FROM staples WHERE source='edhtop16'")
        oids = {r["oracle_id"] for r in rows}
        self.assertIn("6ad8011d-3471-4369-9d68-b264cc027487", oids)
        self.assertIn("oracle-thassa", oids)
        for r in rows:
            self.assertEqual(r["tier"], "cedh")

    def test_respects_threshold(self):
        # 5/100 = 5% (below), 20/100 = 20% (above)
        result = self._run_refresh(
            card_counts={
                "below-threshold": 5,
                "above-threshold": 20,
            },
            total_entries=100,
        )
        self.assertTrue(result.success)

        below = self._query(
            "SELECT * FROM staples WHERE oracle_id='below-threshold'")
        self.assertEqual(len(below), 0)

        above = self._query(
            "SELECT * FROM staples WHERE oracle_id='above-threshold'")
        self.assertGreater(len(above), 0)

    def test_score_is_play_rate(self):
        # 50 decks out of 200 total → play_rate = 0.25
        result = self._run_refresh(
            card_counts={"sol-ring": 50},
            total_entries=200,
        )
        self.assertTrue(result.success)
        rows = self._query("SELECT score FROM staples WHERE oracle_id='sol-ring'")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["score"], 0.25, places=5)

    def test_idempotent(self):
        counts = {"sol-ring-oracle": 50}
        self._run_refresh(counts, total_entries=100)
        result2 = self._run_refresh(counts, total_entries=100)
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

    def test_empty_response_falls_back_to_staples(self):
        """When tournament data is empty, fallback to _fetch_staples."""
        fallback_staples = [
            {"name": "Sol Ring",
             "oracleId": "sol-ring-oracle",
             "colorId": "C", "type": "Artifact",
             "playRateLastYear": 0.50},
        ]
        source = EDHTop16Source()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_tournament_decks",
                                  return_value=({}, 0)):
                    with patch.object(EDHTop16Source, "_fetch_staples",
                                      return_value=fallback_staples):
                        result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertTrue(result.success)
        rows = self._query(
            "SELECT * FROM staples WHERE oracle_id='sol-ring-oracle'")
        self.assertGreater(len(rows), 0, "Fallback staple must be written")

    def test_empty_fallback_preserves_existing_rows(self):
        """Empty tournament + empty staples fallback → existing DB rows survive."""
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.execute(
            "INSERT INTO staples (oracle_id, tier, source, score, last_updated) "
            "VALUES ('existing', 'cedh', 'edhtop16', 0.5, '2026-01-01')"
        )
        conn.commit()
        conn.close()

        source = EDHTop16Source()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_tournament_decks",
                                  return_value=({}, 0)):
                    with patch.object(EDHTop16Source, "_fetch_staples",
                                      return_value=[]):
                        result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertTrue(result.success)
        rows = self._query(
            "SELECT * FROM staples WHERE oracle_id='existing'")
        self.assertGreater(len(rows), 0, "Existing row must survive empty refresh")

    def test_zero_total_entries_does_not_crash(self):
        """total_entries=0 with empty card_counts triggers fallback gracefully."""
        source = EDHTop16Source()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_tournament_decks",
                                  return_value=({}, 0)):
                    with patch.object(EDHTop16Source, "_fetch_staples",
                                      return_value=[]):
                        result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig
        self.assertTrue(result.success)


class TestFetchTournamentDecks(unittest.TestCase):
    """Unit tests for _fetch_tournament_decks pagination logic."""

    @staticmethod
    def _make_page(tids_entries: list[tuple[str, list[list[dict]]]],
                   has_next: bool = False,
                   cursor: str | None = None) -> dict:
        """Build a fake GraphQL tournaments response page.

        tids_entries: [(TID, [maindeck_for_entry1, maindeck_for_entry2, ...]), ...]
        Each maindeck is a list of {oracleId, type} dicts.
        """
        edges = []
        for tid, entry_maindecks in tids_entries:
            entries = [{"maindeck": md} for md in entry_maindecks]
            edges.append({"node": {"TID": tid, "entries": entries}})
        return {
            "data": {
                "tournaments": {
                    "edges": edges,
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": cursor or "",
                    },
                }
            }
        }

    def _run_fetch(self, pages: list[dict]) -> tuple[dict, int]:
        """Run _fetch_tournament_decks with mocked HTTP responses."""
        source = EDHTop16Source()
        responses = [MagicMock() for _ in pages]
        for resp, page in zip(responses, pages):
            resp.raise_for_status = MagicMock()
            resp.json.return_value = page

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.post.side_effect = responses
            return source._fetch_tournament_decks(emit=None, warnings=[])

    def test_counts_cards_across_decks(self):
        sol = {"oracleId": "sol-ring", "type": "Artifact"}
        rhystic = {"oracleId": "rhystic-study", "type": "Enchantment"}
        page = self._make_page([
            ("T1", [[sol, rhystic], [sol]]),   # 2 entries; sol in both, rhystic in 1
            ("T2", [[rhystic]]),               # 1 entry; rhystic only
        ])
        counts, total = self._run_fetch([page])
        self.assertEqual(total, 3)             # 3 top-16 decks total
        self.assertEqual(counts["sol-ring"], 2)
        self.assertEqual(counts["rhystic-study"], 2)

    def test_basic_lands_excluded(self):
        forest = {"oracleId": "basic-forest", "type": "Basic Land \u2014 Forest"}
        sol = {"oracleId": "sol-ring", "type": "Artifact"}
        page = self._make_page([("T1", [[forest, sol]])])
        counts, _ = self._run_fetch([page])
        self.assertNotIn("basic-forest", counts)
        self.assertIn("sol-ring", counts)

    def test_deduplicates_within_deck(self):
        """If a card appears multiple times in one maindeck, count it only once."""
        sol = {"oracleId": "sol-ring", "type": "Artifact"}
        page = self._make_page([("T1", [[sol, sol, sol]])])
        counts, total = self._run_fetch([page])
        self.assertEqual(total, 1)
        self.assertEqual(counts["sol-ring"], 1)

    def test_empty_entries_skipped(self):
        page = self._make_page([("T1", [[], []])])
        counts, total = self._run_fetch([page])
        self.assertEqual(total, 0)
        self.assertEqual(len(counts), 0)

    def test_pagination_merges_results(self):
        sol = {"oracleId": "sol-ring", "type": "Artifact"}
        rhystic = {"oracleId": "rhystic-study", "type": "Enchantment"}
        page1 = self._make_page([("T1", [[sol]])], has_next=True, cursor="c1")
        page2 = self._make_page([("T2", [[rhystic]])])
        counts, total = self._run_fetch([page1, page2])
        self.assertEqual(total, 2)
        self.assertIn("sol-ring", counts)
        self.assertIn("rhystic-study", counts)


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
