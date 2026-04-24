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

import httpx
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
    """Parser tests against the canned staples_response.json fixture."""

    FIXTURE_PATH = (Path(__file__).parent.parent /
                    "fixtures" / "edhtop16" / "staples_response.json")

    def _load_fixture(self):
        if not self.FIXTURE_PATH.exists():
            self.skipTest("edhtop16 fixture not present")
        return json.loads(self.FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_fixture_structure(self):
        data = self._load_fixture()
        self.assertIn("data", data)
        staples = data["data"].get("staples", [])
        self.assertIsInstance(staples, list)
        if staples:
            s = staples[0]
            self.assertIn("oracleId", s)
            self.assertIn("playRateLastYear", s)

    def test_fixture_parser_yields_db_rows(self):
        """_fetch_staples result shapes correctly into staple rows via refresh."""
        data = self._load_fixture()
        staples = data["data"]["staples"]
        # Build a temporary DB and run through the fallback path
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")
        try:
            conn = enrichment_db.get_connection(db_path=db_path)
            conn.close()

            source = EDHTop16Source()
            orig = enrichment_db.DB_PATH
            enrichment_db.DB_PATH = db_path
            try:
                with patch.object(source, "probe", return_value=True):
                    with patch.object(source, "_fetch_tournament_decks",
                                      return_value=({}, 0)):
                        with patch.object(EDHTop16Source, "_fetch_staples",
                                          return_value=staples):
                            result = source.refresh()
            finally:
                enrichment_db.DB_PATH = orig

            self.assertTrue(result.success)

            conn2 = enrichment_db.get_connection(db_path=db_path)
            try:
                rows = conn2.execute(
                    "SELECT oracle_id, score FROM staples "
                    "WHERE source='edhtop16' AND tier='cedh'"
                ).fetchall()
                oracle_ids = {r["oracle_id"] for r in rows}
                # Mystic Remora (91% play rate) should be a cEDH staple
                self.assertIn("8a52f3c0-2552-4425-b2e3-5496eb2232a7", oracle_ids)
                # Every score should be a float between 0 and 1
                for r in rows:
                    self.assertGreaterEqual(r["score"], CEDH_THRESHOLD)
                    self.assertLessEqual(r["score"], 1.0)
            finally:
                conn2.close()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_fixture_basic_land_excluded(self):
        """Parser must exclude any basic land entry from cEDH staples."""
        # Add a basic land to the fixture data in-memory
        data = self._load_fixture()
        staples_with_land = data["data"]["staples"] + [{
            "name": "Forest",
            "oracleId": "basic-forest-oracle-id",
            "colorId": "G",
            "type": "Basic Land \u2014 Forest",
            "playRateLastYear": 0.99,
        }]

        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")
        try:
            conn = enrichment_db.get_connection(db_path=db_path)
            conn.close()

            source = EDHTop16Source()
            orig = enrichment_db.DB_PATH
            enrichment_db.DB_PATH = db_path
            try:
                with patch.object(source, "probe", return_value=True):
                    with patch.object(source, "_fetch_tournament_decks",
                                      return_value=({}, 0)):
                        with patch.object(EDHTop16Source, "_fetch_staples",
                                          return_value=staples_with_land):
                            source.refresh()
            finally:
                enrichment_db.DB_PATH = orig

            conn2 = enrichment_db.get_connection(db_path=db_path)
            try:
                row = conn2.execute(
                    "SELECT * FROM staples WHERE oracle_id='basic-forest-oracle-id'"
                ).fetchone()
                self.assertIsNone(row, "Basic land must not be stored as cEDH staple")
            finally:
                conn2.close()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestEDHTop16RateLimit(unittest.TestCase):
    """Verify 429 handling in _fetch_tournament_decks."""

    def _run_fetch_with_responses(self, status_codes: list[int],
                                  final_data: dict) -> tuple:
        """Run _fetch_tournament_decks where each HTTP call returns the next
        status_code in the list, with the last response returning final_data."""
        from web_enrichment.edhtop16 import RATE_LIMIT_BACKOFF_S
        source = EDHTop16Source()
        call_index = [0]
        sleep_calls = []

        def make_response(status):
            resp = MagicMock()
            resp.status_code = status
            resp.json.return_value = final_data
            if status >= 400:
                resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "Error", request=MagicMock(), response=resp)
            else:
                resp.raise_for_status = MagicMock()
            return resp

        responses = [make_response(sc) for sc in status_codes]

        def side_effect(*args, **kwargs):
            idx = call_index[0]
            call_index[0] += 1
            if idx < len(responses):
                return responses[idx]
            return responses[-1]

        warnings: list[str] = []
        with patch("httpx.Client") as mock_client_cls, \
             patch("time.sleep") as mock_sleep:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.post.side_effect = side_effect
            mock_sleep.side_effect = lambda s: sleep_calls.append(s)
            counts, total = source._fetch_tournament_decks(
                emit=None, warnings=warnings)

        return counts, total, sleep_calls, warnings

    def _one_deck_page(self) -> dict:
        """A valid single-page tournament response with one entry."""
        sol = {"oracleId": "sol-ring", "type": "Artifact"}
        return {
            "data": {
                "tournaments": {
                    "edges": [{"node": {"TID": "T1", "entries": [{"maindeck": [sol]}]}}],
                    "pageInfo": {"hasNextPage": False, "endCursor": ""},
                }
            }
        }

    def test_429_triggers_sleep(self):
        """A 429 response should trigger time.sleep before retry."""
        _, _, sleep_calls, warnings = self._run_fetch_with_responses(
            [429, 200], self._one_deck_page())
        self.assertTrue(len(sleep_calls) >= 1,
                        "Expected at least one sleep call on 429")

    def test_429_sleep_is_backoff(self):
        """First retry sleep should be at least RATE_LIMIT_BACKOFF_S."""
        from web_enrichment.edhtop16 import RATE_LIMIT_BACKOFF_S
        _, _, sleep_calls, _ = self._run_fetch_with_responses(
            [429, 200], self._one_deck_page())
        self.assertGreaterEqual(sleep_calls[0], RATE_LIMIT_BACKOFF_S)

    def test_429_warning_recorded(self):
        """A 429 response should add a warning message."""
        _, _, _, warnings = self._run_fetch_with_responses(
            [429, 200], self._one_deck_page())
        self.assertTrue(any("429" in w or "Rate-limited" in w for w in warnings),
                        "Expected a rate-limit warning")

    def test_success_after_retry(self):
        """After a 429 then a 200, the card should still be counted."""
        counts, total, _, _ = self._run_fetch_with_responses(
            [429, 200], self._one_deck_page())
        self.assertIn("sol-ring", counts)
        self.assertEqual(total, 1)


if __name__ == "__main__":
    unittest.main()
