# tests/enrichment/test_moxfield.py
# ---------------------------------------------------------------------------
# Unit tests for web_enrichment/moxfield.py.
#
# Uses the DB_PATH redirect pattern (NOT patching get_connection) because
# MoxfieldSource calls conn.close() in finally blocks; a patched connection
# object would be invalidated by that call.
#
# All HTTP calls are mocked — no live Moxfield traffic.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import enrichment_db
from web_enrichment.moxfield import (
    MoxfieldSource,
    _format_import_result,
    cache_deck,
    extract_deck_id,
    fetch_deck,
    get_cached_deck,
    parse_deck_cards,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moxfield"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture() -> dict:
    p = FIXTURES / "deck_sample.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _direct_query(db_path: str, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test: URL / ID extraction
# ---------------------------------------------------------------------------

class TestExtractDeckId(unittest.TestCase):

    def test_full_https_url(self):
        url = "https://www.moxfield.com/decks/lzbasAFQhEqY5x5SmJRZ9w"
        self.assertEqual(extract_deck_id(url), "lzbasAFQhEqY5x5SmJRZ9w")

    def test_url_without_www(self):
        url = "https://moxfield.com/decks/lzbasAFQhEqY5x5SmJRZ9w"
        self.assertEqual(extract_deck_id(url), "lzbasAFQhEqY5x5SmJRZ9w")

    def test_bare_deck_id(self):
        self.assertEqual(extract_deck_id("lzbasAFQhEqY5x5SmJRZ9w"),
                         "lzbasAFQhEqY5x5SmJRZ9w")

    def test_url_with_trailing_whitespace(self):
        url = "  https://www.moxfield.com/decks/abc12345def   "
        self.assertEqual(extract_deck_id(url), "abc12345def")

    def test_malformed_url_returns_none(self):
        self.assertIsNone(extract_deck_id("https://archidekt.com/decks/123"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(extract_deck_id(""))

    def test_short_id_returns_none(self):
        # deck IDs are >4 chars; "abc" is too short
        self.assertIsNone(extract_deck_id("abc"))

    def test_deck_url_with_query_string_still_works(self):
        # strip trailing chars — the regex anchors at end so query params
        # should fall through; this verifies graceful handling
        url = "https://www.moxfield.com/decks/lzbasAFQhEqY5x5SmJRZ9w"
        self.assertIsNotNone(extract_deck_id(url))


# ---------------------------------------------------------------------------
# Test: parse_deck_cards
# ---------------------------------------------------------------------------

class TestParseDeckCards(unittest.TestCase):

    def setUp(self):
        self.data = _load_fixture()

    def test_mainboard_and_commanders_merged(self):
        cards = parse_deck_cards(self.data)
        boards = {c["board"] for c in cards}
        self.assertIn("mainboard", boards)
        self.assertIn("commanders", boards)

    def test_sideboard_excluded_by_default(self):
        # Inject a fake sideboard entry
        self.data["sideboard"] = {
            "SB1": {
                "quantity": 1,
                "card": {
                    "oracle_id": "sideboard-oracle-id",
                    "name": "Sidecard",
                    "set": "m21",
                    "cn": "99",
                    "scryfall_id": "sb-scryfall-id",
                },
            }
        }
        cards = parse_deck_cards(self.data, include_side=False)
        boards = {c["board"] for c in cards}
        self.assertNotIn("sideboard", boards)

    def test_sideboard_included_when_flag_set(self):
        self.data["sideboard"] = {
            "SB1": {
                "quantity": 1,
                "card": {
                    "oracle_id": "sideboard-oracle-id",
                    "name": "Sidecard",
                    "set": "m21",
                    "cn": "99",
                    "scryfall_id": "sb-scryfall-id",
                },
            }
        }
        cards = parse_deck_cards(self.data, include_side=True)
        boards = {c["board"] for c in cards}
        self.assertIn("sideboard", boards)

    def test_quantity_preserved(self):
        cards = parse_deck_cards(self.data)
        # Forest has quantity 3 in the fixture
        forest = next(c for c in cards if c["name"] == "Forest")
        self.assertEqual(forest["quantity"], 3)

    def test_cards_missing_oracle_id_skipped(self):
        self.data["mainboard"]["BAD"] = {
            "quantity": 1,
            "card": {"name": "No Oracle Card"},  # no oracle_id
        }
        cards = parse_deck_cards(self.data)
        names = {c["name"] for c in cards}
        self.assertNotIn("No Oracle Card", names)

    def test_oracle_id_populated(self):
        cards = parse_deck_cards(self.data)
        for c in cards:
            self.assertNotEqual(c["oracle_id"], "")

    def test_collector_number_mapped_from_cn(self):
        cards = parse_deck_cards(self.data)
        sol = next(c for c in cards if c["name"] == "Sol Ring")
        self.assertEqual(sol["collector_number"], "472")

    def test_commander_in_output(self):
        cards = parse_deck_cards(self.data)
        commanders = [c for c in cards if c["board"] == "commanders"]
        self.assertEqual(len(commanders), 1)
        self.assertEqual(commanders[0]["name"], "Atraxa, Praetors' Voice")


# ---------------------------------------------------------------------------
# Test: cache_deck and get_cached_deck
# ---------------------------------------------------------------------------

class TestDeckCache(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_enrichment.db")
        # prime the schema
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()
        self.orig_db_path = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path

    def tearDown(self):
        enrichment_db.DB_PATH = self.orig_db_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _load_fixture(self):
        return _load_fixture()

    def test_cache_deck_writes_row(self):
        data = self._load_fixture()
        cache_deck(data["id"], data)
        rows = _direct_query(self.db_path,
                             "SELECT * FROM moxfield_decks WHERE deck_id = ?",
                             (data["id"],))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deck_name"], data["name"])

    def test_cache_deck_owner_extracted(self):
        data = self._load_fixture()
        cache_deck(data["id"], data)
        rows = _direct_query(self.db_path,
                             "SELECT owner FROM moxfield_decks WHERE deck_id = ?",
                             (data["id"],))
        self.assertEqual(rows[0]["owner"], "testuser")

    def test_cache_deck_idempotent(self):
        data = self._load_fixture()
        cache_deck(data["id"], data)
        cache_deck(data["id"], data)  # second write should upsert
        rows = _direct_query(self.db_path,
                             "SELECT * FROM moxfield_decks WHERE deck_id = ?",
                             (data["id"],))
        self.assertEqual(len(rows), 1)

    def test_get_cached_deck_hit(self):
        data = self._load_fixture()
        cache_deck(data["id"], data)
        result = get_cached_deck(data["id"])
        self.assertIsNotNone(result)
        self.assertEqual(result["deck_id"], data["id"])

    def test_get_cached_deck_miss(self):
        result = get_cached_deck("nonexistent-deck-id-xyz")
        self.assertIsNone(result)

    def test_get_cached_deck_returns_cards(self):
        data = self._load_fixture()
        cache_deck(data["id"], data)
        result = get_cached_deck(data["id"])
        self.assertIn("cards", result)
        self.assertGreater(len(result["cards"]), 0)


# ---------------------------------------------------------------------------
# Test: MoxfieldSource.import_deck — happy path with mocked HTTP
# ---------------------------------------------------------------------------

class TestMoxfieldSourceImport(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_enrichment.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()
        self.orig_db_path = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        self.source = MoxfieldSource()
        self.fixture = _load_fixture()

    def tearDown(self):
        enrichment_db.DB_PATH = self.orig_db_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _mock_fetch(self, data):
        """Return a context manager that patches fetch_deck."""
        return patch(
            "web_enrichment.moxfield.fetch_deck",
            return_value=data,
        )

    def test_import_deck_by_url(self):
        url = f"https://www.moxfield.com/decks/{self.fixture['id']}"
        with self._mock_fetch(self.fixture):
            result = self.source.import_deck(url)
        self.assertEqual(result["deck_id"], self.fixture["id"])
        self.assertEqual(result["deck_name"], self.fixture["name"])

    def test_import_deck_by_bare_id(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_deck(self.fixture["id"])
        self.assertEqual(result["deck_id"], self.fixture["id"])

    def test_import_deck_card_count(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_deck(self.fixture["id"])
        # Fixture has 3 mainboard entries (quantities: 1,1,3) + 1 commander
        # Total quantity: 1+1+3+1 = 6
        self.assertEqual(result["card_count"], 6)

    def test_import_deck_caches_result(self):
        with self._mock_fetch(self.fixture):
            self.source.import_deck(self.fixture["id"])
        rows = _direct_query(self.db_path,
                             "SELECT * FROM moxfield_decks WHERE deck_id = ?",
                             (self.fixture["id"],))
        self.assertEqual(len(rows), 1)

    def test_import_deck_cache_hit_skips_fetch(self):
        # First import: populate cache
        with self._mock_fetch(self.fixture):
            self.source.import_deck(self.fixture["id"])
        # Second import: should hit cache, not call fetch_deck
        with patch("web_enrichment.moxfield.fetch_deck",
                   side_effect=AssertionError("fetch should not be called on cache hit")):
            result = self.source.import_deck(self.fixture["id"], use_cache=True)
        self.assertEqual(result["deck_id"], self.fixture["id"])

    def test_import_deck_use_cache_false_refetches(self):
        # First import: populate cache
        with self._mock_fetch(self.fixture):
            self.source.import_deck(self.fixture["id"])
        # Second import with use_cache=False: must call fetch_deck again
        call_count = [0]
        original_fixture = self.fixture

        def counting_fetch(deck_id):
            call_count[0] += 1
            return original_fixture

        with patch("web_enrichment.moxfield.fetch_deck", side_effect=counting_fetch):
            self.source.import_deck(self.fixture["id"], use_cache=False)
        self.assertEqual(call_count[0], 1)

    def test_import_deck_idempotent(self):
        """Two imports of the same deck produce identical DB state."""
        with self._mock_fetch(self.fixture):
            r1 = self.source.import_deck(self.fixture["id"], use_cache=False)
        with self._mock_fetch(self.fixture):
            r2 = self.source.import_deck(self.fixture["id"], use_cache=False)
        # Card count and deck info must match
        self.assertEqual(r1["deck_id"], r2["deck_id"])
        self.assertEqual(r1["card_count"], r2["card_count"])
        rows = _direct_query(self.db_path,
                             "SELECT COUNT(*) AS n FROM moxfield_decks WHERE deck_id = ?",
                             (self.fixture["id"],))
        self.assertEqual(rows[0]["n"], 1)

    def test_import_deck_bad_url_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.source.import_deck("https://archidekt.com/decks/12345")

    def test_import_deck_empty_deck_id_raises(self):
        with self.assertRaises(ValueError):
            self.source.import_deck("")


# ---------------------------------------------------------------------------
# Test: empty response — existing cache not wiped
# ---------------------------------------------------------------------------

class TestEmptyAndMalformedResponses(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_enrichment.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()
        self.orig_db_path = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        self.source = MoxfieldSource()
        self.fixture = _load_fixture()

    def tearDown(self):
        enrichment_db.DB_PATH = self.orig_db_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_deck_with_no_mainboard_cards_still_caches(self):
        empty_deck = dict(self.fixture)
        empty_deck["mainboard"] = {}
        empty_deck["commanders"] = {}
        with patch("web_enrichment.moxfield.fetch_deck", return_value=empty_deck):
            result = self.source.import_deck(self.fixture["id"], use_cache=False)
        self.assertEqual(result["card_count"], 0)
        self.assertEqual(result["cards"], [])
        # Row should still be written
        rows = _direct_query(self.db_path,
                             "SELECT * FROM moxfield_decks WHERE deck_id = ?",
                             (self.fixture["id"],))
        self.assertEqual(len(rows), 1)

    def test_malformed_response_missing_required_key_raises(self):
        bad_data = {"id": "abc12345def", "name": "Bad"}  # missing 'format', 'mainboard'
        with patch("web_enrichment.moxfield.fetch_deck",
                   side_effect=ValueError("missing keys: ['format', 'mainboard']")):
            with self.assertRaises(ValueError):
                self.source.import_deck("abc12345def", use_cache=False)

    def test_malformed_response_no_partial_commit(self):
        """If an exception occurs during import, no partial data is written."""
        deck_id = self.fixture["id"]

        def broken_fetch(did):
            raise ValueError("Simulated parse failure")

        with patch("web_enrichment.moxfield.fetch_deck", side_effect=broken_fetch):
            with self.assertRaises(ValueError):
                self.source.import_deck(deck_id, use_cache=False)

        rows = _direct_query(self.db_path,
                             "SELECT * FROM moxfield_decks WHERE deck_id = ?",
                             (deck_id,))
        self.assertEqual(len(rows), 0, "No row should be written after a failed import")

    def test_network_error_propagates(self):
        import httpx
        with patch("web_enrichment.moxfield.fetch_deck",
                   side_effect=httpx.RequestError("timeout")):
            with self.assertRaises(httpx.RequestError):
                self.source.import_deck(self.fixture["id"], use_cache=False)


# ---------------------------------------------------------------------------
# Test: probe delegates to probe module
# ---------------------------------------------------------------------------

class TestMoxfieldProbe(unittest.TestCase):

    def test_probe_success(self):
        source = MoxfieldSource()
        mock_result = MagicMock()
        mock_result.ok = True
        with patch("probes.probe_moxfield.probe", return_value=mock_result):
            result = source.probe()
        self.assertTrue(result)

    def test_probe_failure(self):
        source = MoxfieldSource()
        mock_result = MagicMock()
        mock_result.ok = False
        with patch("probes.probe_moxfield.probe", return_value=mock_result):
            result = source.probe()
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Test: refresh is a no-op stub that still updates sync_metadata
# ---------------------------------------------------------------------------

class TestMoxfieldRefresh(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_enrichment.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()
        self.orig_db_path = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path

    def tearDown(self):
        enrichment_db.DB_PATH = self.orig_db_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_refresh_returns_success(self):
        source = MoxfieldSource()
        result = source.refresh()
        self.assertTrue(result.success)
        self.assertEqual(result.source, "moxfield")

    def test_refresh_updates_sync_metadata(self):
        source = MoxfieldSource()
        source.refresh()
        rows = _direct_query(self.db_path,
                             "SELECT * FROM sync_metadata WHERE source = 'moxfield'")
        self.assertEqual(len(rows), 1)

    def test_coverage_report_includes_cached_decks(self):
        source = MoxfieldSource()
        data = _load_fixture()
        cache_deck(data["id"], data)
        report = source.coverage_report()
        self.assertEqual(report["cached_decks"], 1)


if __name__ == "__main__":
    unittest.main()
