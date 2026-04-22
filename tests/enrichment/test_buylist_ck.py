# tests/enrichment/test_buylist_ck.py
# ---------------------------------------------------------------------------
# Tests for CardKingdomBuylistSource (web_enrichment/buylist_ck.py).
#
# All tests use the enrichment_db.DB_PATH redirect pattern — do NOT patch
# get_connection directly (sources call conn.close() in finally, which
# invalidates a patched connection object).
#
# Fixture HTML lives at tests/fixtures/buylist_ck/buylist_page.html.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import enrichment_db
from web_enrichment.buylist_ck import (
    CardKingdomBuylistSource,
    _parse_html_rows,
    _parse_price,
    _build_name_index,
    _resolve_oracle_id,
    VENDOR,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "buylist_ck"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _direct_query(db_path: str, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _fixture_html() -> str:
    p = FIXTURES / "buylist_page.html"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helper: minimal fake raw_rows that cover multiple resolved cards
# ---------------------------------------------------------------------------

_FAKE_RAW_ROWS = [
    {"name": "Sol Ring",      "set_code": "cmr", "price": "$1.50"},
    {"name": "Lightning Bolt","set_code": "m11", "price": "2.50"},
    {"name": "Counterspell",  "set_code": "tmp", "price": "$0.75"},
]


def _fake_name_index():
    """Minimal name index that resolves the three test cards above."""
    name_index = {
        "sol ring":       "oracle-sol-ring",
        "lightning bolt": "oracle-lightning-bolt",
        "counterspell":   "oracle-counterspell",
    }
    name_set_index = {
        ("sol ring",       "cmr"): "oracle-sol-ring",
        ("lightning bolt", "m11"): "oracle-lightning-bolt",
        ("counterspell",   "tmp"): "oracle-counterspell",
    }
    return name_index, name_set_index


# ---------------------------------------------------------------------------
# Unit tests: _parse_price
# ---------------------------------------------------------------------------

class TestParsePrice(unittest.TestCase):

    def test_dollar_string(self):
        self.assertAlmostEqual(_parse_price("$1.50"), 1.50)

    def test_bare_float_string(self):
        self.assertAlmostEqual(_parse_price("2.50"), 2.50)

    def test_numeric_float(self):
        self.assertAlmostEqual(_parse_price(0.75), 0.75)

    def test_integer(self):
        self.assertAlmostEqual(_parse_price(3), 3.0)

    def test_zero(self):
        self.assertAlmostEqual(_parse_price("$0.00"), 0.0)

    def test_negative_returns_none(self):
        self.assertIsNone(_parse_price("-1.00"))

    def test_none_returns_none(self):
        self.assertIsNone(_parse_price(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_price(""))

    def test_non_numeric_returns_none(self):
        self.assertIsNone(_parse_price("N/A"))

    def test_whitespace_stripped(self):
        self.assertAlmostEqual(_parse_price("  $1.25  "), 1.25)


# ---------------------------------------------------------------------------
# Unit tests: HTML parsing (_parse_html_rows)
# ---------------------------------------------------------------------------

class TestParseHtmlRows(unittest.TestCase):

    def setUp(self):
        html = _fixture_html()
        if not html:
            self.skipTest("Fixture HTML not found")
        from bs4 import BeautifulSoup
        self.soup = BeautifulSoup(html, "html.parser")

    def test_returns_list(self):
        rows = _parse_html_rows(self.soup)
        self.assertIsInstance(rows, list)

    def test_parses_sol_ring(self):
        rows = _parse_html_rows(self.soup)
        names = [r["name"] for r in rows]
        self.assertIn("Sol Ring", names)

    def test_parses_correct_price(self):
        rows = _parse_html_rows(self.soup)
        sol = next((r for r in rows if r["name"] == "Sol Ring"), None)
        self.assertIsNotNone(sol)
        self.assertEqual(sol["price"], "$1.50")

    def test_parses_set_code(self):
        rows = _parse_html_rows(self.soup)
        sol = next((r for r in rows if r["name"] == "Sol Ring"), None)
        self.assertIsNotNone(sol)
        self.assertEqual(sol["set_code"], "cmr")

    def test_parses_multiple_rows(self):
        rows = _parse_html_rows(self.soup)
        # Fixture has 6 rows (5 real cards + 1 bogus)
        self.assertGreaterEqual(len(rows), 5)

    def test_empty_html_returns_empty(self):
        from bs4 import BeautifulSoup
        rows = _parse_html_rows(BeautifulSoup("<html></html>", "html.parser"))
        self.assertEqual(rows, [])


# ---------------------------------------------------------------------------
# Unit tests: name resolution
# ---------------------------------------------------------------------------

class TestResolveOracleId(unittest.TestCase):

    def setUp(self):
        self.name_index, self.name_set_index = _fake_name_index()

    def test_set_specific_match(self):
        oid = _resolve_oracle_id(
            "Sol Ring", "cmr", self.name_index, self.name_set_index)
        self.assertEqual(oid, "oracle-sol-ring")

    def test_name_only_fallback(self):
        oid = _resolve_oracle_id(
            "Sol Ring", "UNKNOWN", self.name_index, self.name_set_index)
        self.assertEqual(oid, "oracle-sol-ring")

    def test_bogus_card_returns_none(self):
        oid = _resolve_oracle_id(
            "ZZZZZ_Bogus_Card_ZZZZZ", "fake",
            self.name_index, self.name_set_index)
        self.assertIsNone(oid)

    def test_case_insensitive(self):
        oid = _resolve_oracle_id(
            "LIGHTNING BOLT", "M11", self.name_index, self.name_set_index)
        self.assertEqual(oid, "oracle-lightning-bolt")


# ---------------------------------------------------------------------------
# Probe delegation test
# ---------------------------------------------------------------------------

class TestProbeDelegation(unittest.TestCase):

    def test_probe_delegates_to_probe_module(self):
        """probe() returns .ok from probes.probe_buylist_ck.probe()."""
        source = CardKingdomBuylistSource()
        mock_result = MagicMock()
        mock_result.ok = True
        with patch("probes.probe_buylist_ck.probe", return_value=mock_result):
            self.assertTrue(source.probe())

    def test_probe_returns_false_when_probe_module_returns_not_ok(self):
        source = CardKingdomBuylistSource()
        mock_result = MagicMock()
        mock_result.ok = False
        with patch("probes.probe_buylist_ck.probe", return_value=mock_result):
            self.assertFalse(source.probe())


# ---------------------------------------------------------------------------
# Refresh: happy path writes rows
# ---------------------------------------------------------------------------

class TestRefreshWritesRows(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, raw_rows, name_index=None, name_set_index=None):
        source = CardKingdomBuylistSource()
        ni, nsi = name_index, name_set_index
        if ni is None:
            ni, nsi = _fake_name_index()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_all_rows",
                                  return_value=raw_rows):
                    with patch(
                        "web_enrichment.buylist_ck._build_name_index",
                        return_value=(ni, nsi)
                    ):
                        return source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

    def test_happy_path_writes_correct_rows(self):
        result = self._run(_FAKE_RAW_ROWS)
        self.assertTrue(result.success, result.errors)
        rows = _direct_query(
            self.db_path,
            "SELECT oracle_id, price_usd FROM buylists WHERE vendor='ck'"
        )
        oracle_ids = {r["oracle_id"] for r in rows}
        self.assertIn("oracle-sol-ring", oracle_ids)
        self.assertIn("oracle-lightning-bolt", oracle_ids)
        self.assertIn("oracle-counterspell", oracle_ids)

    def test_happy_path_prices_correct(self):
        result = self._run(_FAKE_RAW_ROWS)
        self.assertTrue(result.success)
        row = _direct_query(
            self.db_path,
            "SELECT price_usd FROM buylists WHERE oracle_id='oracle-sol-ring' "
            "AND vendor='ck'"
        )
        self.assertEqual(len(row), 1)
        self.assertAlmostEqual(row[0]["price_usd"], 1.50, places=2)

    def test_happy_path_sync_metadata_updated(self):
        self._run(_FAKE_RAW_ROWS)
        meta = _direct_query(
            self.db_path,
            "SELECT * FROM sync_metadata WHERE source='buylist_ck'"
        )
        self.assertEqual(len(meta), 1)
        self.assertIsNotNone(meta[0]["last_success"])

    def test_rows_changed_positive(self):
        result = self._run(_FAKE_RAW_ROWS)
        self.assertGreater(result.rows_changed, 0)


# ---------------------------------------------------------------------------
# Refresh: empty response (graceful, no data wipe)
# ---------------------------------------------------------------------------

class TestRefreshEmptyResponse(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        # Pre-seed a row so we can confirm it's preserved
        conn.execute(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES ('oracle-existing', 'ck', 5.00, '2026-01-01')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_rows_does_not_wipe(self):
        source = CardKingdomBuylistSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_all_rows", return_value=[]):
                    with patch(
                        "web_enrichment.buylist_ck._build_name_index",
                        return_value=_fake_name_index()
                    ):
                        result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

        # Refresh should fail (empty rows detected) without wiping existing data
        self.assertFalse(result.success)
        rows = _direct_query(
            self.db_path,
            "SELECT oracle_id FROM buylists WHERE vendor='ck'"
        )
        oids = {r["oracle_id"] for r in rows}
        self.assertIn("oracle-existing", oids, "Pre-existing rows must survive")

    def test_empty_response_has_warning(self):
        source = CardKingdomBuylistSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_all_rows", return_value=[]):
                    with patch(
                        "web_enrichment.buylist_ck._build_name_index",
                        return_value=_fake_name_index()
                    ):
                        result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

        all_msgs = result.errors + result.warnings
        self.assertTrue(any("zero rows" in m.lower() or "abort" in m.lower()
                            for m in all_msgs))


# ---------------------------------------------------------------------------
# Refresh: transactional safety (mid-write failure leaves no partial rows)
# ---------------------------------------------------------------------------

class TestRefreshTransactional(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_db_error_leaves_no_partial_rows(self):
        """If _write raises, no rows should be committed."""
        source = CardKingdomBuylistSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_all_rows",
                                  return_value=_FAKE_RAW_ROWS):
                    with patch(
                        "web_enrichment.buylist_ck._build_name_index",
                        return_value=_fake_name_index()
                    ):
                        with patch.object(
                            CardKingdomBuylistSource, "_write",
                            side_effect=RuntimeError("simulated DB crash")
                        ):
                            result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertFalse(result.success)
        rows = _direct_query(
            self.db_path,
            "SELECT COUNT(*) as cnt FROM buylists WHERE vendor='ck'"
        )
        self.assertEqual(rows[0]["cnt"], 0, "No rows should be committed")


# ---------------------------------------------------------------------------
# Refresh: unresolvable names go to warnings, not rows
# ---------------------------------------------------------------------------

class TestRefreshDropsUnresolvableNames(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bogus_name_in_warnings_not_rows(self):
        raw_rows = [
            {"name": "Sol Ring", "set_code": "cmr", "price": "$1.50"},
            {"name": "ZZZZZ_Bogus_ZZZZZ", "set_code": "fake", "price": "$5.00"},
        ]
        source = CardKingdomBuylistSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_all_rows",
                                  return_value=raw_rows):
                    with patch(
                        "web_enrichment.buylist_ck._build_name_index",
                        return_value=_fake_name_index()
                    ):
                        result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertTrue(result.success)
        # Only Sol Ring should be in the DB
        rows = _direct_query(
            self.db_path,
            "SELECT oracle_id FROM buylists WHERE vendor='ck'"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["oracle_id"], "oracle-sol-ring")

        # Warning should mention the bogus name
        self.assertTrue(
            any("ZZZZZ_Bogus_ZZZZZ" in w for w in result.warnings),
            f"Expected unresolved name warning, got: {result.warnings}")

    def test_all_unresolvable_returns_failure_not_crash(self):
        raw_rows = [
            {"name": "AAAAA_Bogus1", "set_code": "fake", "price": "$1.00"},
            {"name": "BBBBB_Bogus2", "set_code": "fake", "price": "$2.00"},
        ]
        source = CardKingdomBuylistSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_all_rows",
                                  return_value=raw_rows):
                    # Empty indexes = nothing resolves
                    with patch(
                        "web_enrichment.buylist_ck._build_name_index",
                        return_value=({}, {})
                    ):
                        result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

        # zero resolved rows -> "zero rows" abort (same as empty response)
        self.assertFalse(result.success)


# ---------------------------------------------------------------------------
# Refresh: idempotency (two refreshes → identical DB state)
# ---------------------------------------------------------------------------

class TestRefreshIdempotent(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        source = CardKingdomBuylistSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_all_rows",
                                  return_value=_FAKE_RAW_ROWS):
                    with patch(
                        "web_enrichment.buylist_ck._build_name_index",
                        return_value=_fake_name_index()
                    ):
                        return source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

    def test_two_refreshes_identical_row_count(self):
        r1 = self._run()
        r2 = self._run()
        self.assertTrue(r1.success)
        self.assertTrue(r2.success)
        rows = _direct_query(
            self.db_path,
            "SELECT COUNT(*) as cnt FROM buylists WHERE vendor='ck'"
        )
        self.assertEqual(rows[0]["cnt"], len(_FAKE_RAW_ROWS))

    def test_two_refreshes_identical_prices(self):
        self._run()
        self._run()
        rows = _direct_query(
            self.db_path,
            "SELECT oracle_id, price_usd FROM buylists WHERE vendor='ck' "
            "ORDER BY oracle_id"
        )
        self.assertEqual(len(rows), len(_FAKE_RAW_ROWS))
        # Sol Ring price should be 1.50 after both runs
        sol = next((r for r in rows if r["oracle_id"] == "oracle-sol-ring"), None)
        self.assertIsNotNone(sol)
        self.assertAlmostEqual(sol["price_usd"], 1.50, places=2)


# ---------------------------------------------------------------------------
# coverage_report: fast, no network
# ---------------------------------------------------------------------------

class TestCoverageReportFast(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.execute(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES ('oid-a', 'ck', 2.50, '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES ('oid-b', 'ck', 1.00, '2026-01-01')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_coverage_report_fast(self):
        source = CardKingdomBuylistSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            t0 = time.monotonic()
            report = source.coverage_report()
            elapsed = time.monotonic() - t0
        finally:
            enrichment_db.DB_PATH = orig

        self.assertLess(elapsed, 0.1, "coverage_report must complete in < 100ms")
        self.assertEqual(report["source"], "buylist_ck")
        self.assertEqual(report["row_count"], 2)
        self.assertAlmostEqual(report["avg_price_usd"], 1.75, places=2)

    def test_coverage_report_no_network(self):
        """coverage_report must not hit the network even if probe would."""
        source = CardKingdomBuylistSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch("httpx.get",
                       side_effect=AssertionError("network called")):
                with patch("httpx.Client",
                           side_effect=AssertionError("network called")):
                    # Should not raise
                    report = source.coverage_report()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertEqual(report["row_count"], 2)


# ---------------------------------------------------------------------------
# Probe failure aborts refresh
# ---------------------------------------------------------------------------

class TestProbeFails(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_probe_failure_aborts_refresh(self):
        source = CardKingdomBuylistSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=False):
                result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertFalse(result.success)
        self.assertTrue(any("probe" in e.lower() for e in result.errors))
        rows = _direct_query(
            self.db_path,
            "SELECT COUNT(*) as cnt FROM buylists WHERE vendor='ck'"
        )
        self.assertEqual(rows[0]["cnt"], 0)


# ---------------------------------------------------------------------------
# Query token: buylist:ck presence and comparison
# ---------------------------------------------------------------------------

class TestQueryTokenBuylistCk(unittest.TestCase):
    """Test buylist:ck query token integration via query_parser."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.execute(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES ('oid-test', 'ck', 2.50, '2026-01-01')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _enrichment_data(self, price):
        return {"buylist_ck_price": price}

    def test_buylist_ck_ge_price_matches(self):
        from query_parser import matches_query
        enr = self._enrichment_data(2.50)
        card = {"oracle_id": "oid-test"}
        self.assertTrue(matches_query("buylist:ck>=1.00", card,
                                      enrichment_data=enr))

    def test_buylist_ck_ge_price_no_match(self):
        from query_parser import matches_query
        enr = self._enrichment_data(2.50)
        card = {"oracle_id": "oid-test"}
        self.assertFalse(matches_query("buylist:ck>=5.00", card,
                                       enrichment_data=enr))

    def test_buylist_ck_lt_price(self):
        from query_parser import matches_query
        enr = self._enrichment_data(0.50)
        card = {"oracle_id": "oid-test"}
        self.assertTrue(matches_query("buylist:ck<1.00", card,
                                      enrichment_data=enr))

    def test_buylist_ck_presence_matches_nonzero(self):
        from query_parser import matches_query
        enr = self._enrichment_data(0.25)
        card = {"oracle_id": "oid-test"}
        self.assertTrue(matches_query("buylist:ck", card,
                                      enrichment_data=enr))

    def test_buylist_ck_presence_no_match_on_none(self):
        from query_parser import matches_query
        enr = self._enrichment_data(None)
        card = {"oracle_id": "oid-test"}
        self.assertFalse(matches_query("buylist:ck", card,
                                       enrichment_data=enr))

    def test_buylist_ck_presence_no_match_on_zero(self):
        from query_parser import matches_query
        enr = self._enrichment_data(0.0)
        card = {"oracle_id": "oid-test"}
        self.assertFalse(matches_query("buylist:ck", card,
                                       enrichment_data=enr))

    def test_negative_buylist_ck_matches_absent(self):
        from query_parser import matches_query
        enr = self._enrichment_data(None)
        card = {"oracle_id": "oid-test"}
        self.assertTrue(matches_query("-buylist:ck", card,
                                      enrichment_data=enr))

    def test_negative_buylist_ck_no_match_when_present(self):
        from query_parser import matches_query
        enr = self._enrichment_data(2.50)
        card = {"oracle_id": "oid-test"}
        self.assertFalse(matches_query("-buylist:ck", card,
                                       enrichment_data=enr))

    def test_no_enrichment_data_returns_false(self):
        from query_parser import matches_query
        card = {"oracle_id": "oid-test"}
        self.assertFalse(matches_query("buylist:ck>=1.00", card))

    def test_collect_enrichment_fields_includes_buylist(self):
        from query_parser import parse_query, collect_enrichment_fields
        ast = parse_query("buylist:ck>=1.00")
        fields = collect_enrichment_fields(ast)
        self.assertIn("buylist", fields)


# ---------------------------------------------------------------------------
# Query token: end-to-end with sort_config._get_enrichment_data
# ---------------------------------------------------------------------------

class TestSortConfigBuylistToken(unittest.TestCase):
    """End-to-end: insert a row, exercise sort_config._get_enrichment_data."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.execute(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES ('oid-fixture', 'ck', 2.50, '2026-01-01')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get_enr(self, oracle_id: str) -> dict:
        """Call sort_config._get_enrichment_data with redirected DB_PATH."""
        from sort_config import SortConfig
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            config = SortConfig(1, 1, [])
            return config._get_enrichment_data(oracle_id)
        finally:
            enrichment_db.DB_PATH = orig

    def test_buylist_ck_price_in_enrichment_data(self):
        data = self._get_enr("oid-fixture")
        self.assertIn("buylist_ck_price", data)
        self.assertAlmostEqual(data["buylist_ck_price"], 2.50, places=2)

    def test_absent_card_returns_none_price(self):
        data = self._get_enr("oid-unknown")
        # Either key absent or value is None
        price = data.get("buylist_ck_price")
        self.assertIsNone(price)

    def test_buylist_ck_ge_matches_via_sort_config(self):
        from query_parser import parse_query, evaluate_query
        ast = parse_query("buylist:ck>=1.00")
        data = self._get_enr("oid-fixture")
        card = {"oracle_id": "oid-fixture"}
        self.assertTrue(evaluate_query(ast, card, enrichment_data=data))

    def test_buylist_ck_ge_no_match_via_sort_config(self):
        from query_parser import parse_query, evaluate_query
        ast = parse_query("buylist:ck>=5.00")
        data = self._get_enr("oid-fixture")
        card = {"oracle_id": "oid-fixture"}
        self.assertFalse(evaluate_query(ast, card, enrichment_data=data))


# ---------------------------------------------------------------------------
# Coverage delta warning (>10% row-count drop triggers warning)
# ---------------------------------------------------------------------------

class TestNegativeDeltaWarning(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        # Pre-seed 1000 rows so the delta to ~100 rows is ~90%
        conn.executemany(
            "INSERT INTO buylists (oracle_id, vendor, price_usd, last_updated) "
            "VALUES (?, 'ck', 1.00, '2026-01-01')",
            [(f"oid-{i}",) for i in range(1000)]
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_large_row_count_drop_emits_warning(self):
        # Only return 10 resolved rows (1% of 1000 pre-existing)
        small_raw_rows = [
            {"name": "Sol Ring",       "set_code": "cmr", "price": "$1.50"},
            {"name": "Lightning Bolt", "set_code": "m11", "price": "$2.50"},
            {"name": "Counterspell",   "set_code": "tmp", "price": "$0.75"},
        ]
        source = CardKingdomBuylistSource()
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            with patch.object(source, "probe", return_value=True):
                with patch.object(source, "_fetch_all_rows",
                                  return_value=small_raw_rows):
                    with patch(
                        "web_enrichment.buylist_ck._build_name_index",
                        return_value=_fake_name_index()
                    ):
                        result = source.refresh()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertTrue(result.success)
        self.assertTrue(
            any("%" in w or "delta" in w.lower() or "->" in w
                for w in result.warnings),
            f"Expected delta warning, got: {result.warnings}")


# ---------------------------------------------------------------------------
# Rate limit / 429 simulation: exponential backoff via fetch method
# ---------------------------------------------------------------------------

class TestRateLimitHandling(unittest.TestCase):
    """The JSON API _fetch_json_api should back off on 429 and continue."""

    def test_429_response_causes_retry(self):
        """Feed a 429 then a valid response; should return rows."""
        source = CardKingdomBuylistSource()
        call_count = [0]

        def fake_get(url, params=None):
            call_count[0] += 1
            mock_resp = MagicMock()
            if call_count[0] == 1:
                mock_resp.status_code = 429
                mock_resp.is_success = False
                return mock_resp
            # Second call: valid single-page response
            mock_resp.status_code = 200
            mock_resp.is_success = True
            mock_resp.json.return_value = {
                "data": [
                    {"name": "Sol Ring", "setCode": "cmr", "buyPrice": 1.50}
                ],
                "meta": {"current_page": 1, "last_page": 1},
            }
            return mock_resp

        mock_client = MagicMock()
        mock_client.get.side_effect = fake_get
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("httpx.Client", return_value=mock_client):
            with patch("time.sleep"):  # don't actually sleep
                rows = source._fetch_json_api(emit=None, warnings=[])

        self.assertGreater(len(rows), 0)
        self.assertEqual(rows[0]["name"], "Sol Ring")


if __name__ == "__main__":
    unittest.main()
