# tests/probes/test_tagger_probe.py
# ---------------------------------------------------------------------------
# Unit tests for probes/probe_tagger.py.
#
# Exercises both:
#   - The probe() function under PROBES_OFFLINE=1 (no network)
#   - The _try_search_fallback() helper with a canned fixture response
#   - Shape-assert on the Scryfall search API response fixture
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIXTURES_DIR = _ROOT / "tests" / "fixtures" / "tagger"


class TestProbeTaggerOffline(unittest.TestCase):
    """probe() with PROBES_OFFLINE=1 must return ok=True and skip network."""

    def test_offline_mode_returns_ok_true(self):
        with patch.dict(os.environ, {"PROBES_OFFLINE": "1"}):
            from probes.probe_tagger import probe
            result = probe()
        self.assertTrue(result.ok)
        self.assertTrue(
            any("PROBES_OFFLINE" in w for w in result.warnings),
            "Expected PROBES_OFFLINE warning"
        )

    def test_offline_mode_no_network_calls(self):
        """Confirm no httpx calls occur when offline."""
        with patch.dict(os.environ, {"PROBES_OFFLINE": "1"}):
            with patch("httpx.get") as mock_get:
                with patch("httpx.post") as mock_post:
                    from probes.probe_tagger import probe
                    probe()
                    mock_get.assert_not_called()
                    mock_post.assert_not_called()


class TestTrySearchFallback(unittest.TestCase):
    """Unit-test _try_search_fallback() against canned fixture data."""

    def _make_mock_response(self, data: dict, status_code: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.is_success = (200 <= status_code < 300)
        resp.json.return_value = data
        resp.headers = {}
        return resp

    def _load_fixture(self, name: str) -> dict:
        path = FIXTURES_DIR / name
        return json.loads(path.read_text(encoding="utf-8"))

    def test_fallback_happy_path(self):
        """Returns ok=True with sample_sizes populated from fixture data."""
        fixture = self._load_fixture("scryfall_search_otag_removal.json")

        # Build a mock client that returns the fixture for any GET
        mock_resp = self._make_mock_response(fixture)
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("probes.probe_tagger.httpx.Client",
                   return_value=mock_client):
            from probes.probe_tagger import _try_search_fallback
            ok, summary, warnings = _try_search_fallback()

        self.assertTrue(ok)
        self.assertEqual(summary["path_used"], "scryfall_search_api_fallback")
        self.assertIn("removal", summary["sample_sizes"])
        self.assertEqual(summary["sample_sizes"]["removal"],
                         fixture["total_cards"])
        self.assertIsInstance(summary["ratelimit_headers"], dict)

    def test_fallback_empty_results(self):
        """tag returning 0 results is not an error by itself (tag may be empty)."""
        empty_resp = self._make_mock_response({
            "object": "list",
            "total_cards": 0,
            "has_more": False,
            "data": [],
        })
        mock_client = MagicMock()
        mock_client.get.return_value = empty_resp
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("probes.probe_tagger.httpx.Client",
                   return_value=mock_client):
            from probes.probe_tagger import _try_search_fallback
            ok, summary, warnings = _try_search_fallback()

        # All samples returned 0 → ok should be False (unexpected)
        self.assertFalse(ok)
        self.assertTrue(
            any("0 results" in w for w in warnings),
            f"Expected '0 results' warning, got: {warnings}"
        )

    def test_fallback_404_tag_not_found(self):
        """404 response means tag has no cards — recorded as 0, not an error."""
        mock_resp = self._make_mock_response({}, status_code=404)
        mock_resp.is_success = False
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("probes.probe_tagger.httpx.Client",
                   return_value=mock_client):
            from probes.probe_tagger import _try_search_fallback
            ok, summary, warnings = _try_search_fallback()

        # With all 404s, sample_sizes will all be 0 → ok=False
        self.assertFalse(ok)
        # But no exception raised
        self.assertIsInstance(summary, dict)

    def test_fallback_network_error_returns_not_ok(self):
        """Network exception produces ok=False with an informative warning."""
        mock_client = MagicMock()
        mock_client.get.side_effect = Exception("connection refused")
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("probes.probe_tagger.httpx.Client",
                   return_value=mock_client):
            from probes.probe_tagger import _try_search_fallback
            ok, summary, warnings = _try_search_fallback()

        self.assertFalse(ok)
        self.assertTrue(
            any("connection refused" in w for w in warnings),
            f"Expected connection error in warnings, got: {warnings}"
        )

    def test_fallback_429_rate_limit_warning(self):
        """429 response emits a rate-limit warning and aborts gracefully."""
        mock_resp = self._make_mock_response({}, status_code=429)
        mock_resp.is_success = False
        mock_resp.headers = {"Retry-After": "2"}
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("probes.probe_tagger.httpx.Client",
                   return_value=mock_client):
            with patch("time.sleep"):  # skip actual sleep
                from probes.probe_tagger import _try_search_fallback
                ok, summary, warnings = _try_search_fallback()

        self.assertFalse(ok)
        self.assertTrue(
            any("429" in w or "Rate-limit" in w or "rate-limit" in w.lower()
                for w in warnings),
            f"Expected rate-limit warning, got: {warnings}"
        )

    def test_fallback_captures_ratelimit_headers(self):
        """Rate-limit headers present in response are captured in summary."""
        fixture = self._load_fixture("scryfall_search_otag_removal.json")
        mock_resp = self._make_mock_response(fixture)
        mock_resp.headers = {
            "X-RateLimit-Remaining": "99",
            "X-RateLimit-Limit": "100",
            "Content-Type": "application/json",
        }
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: s
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("probes.probe_tagger.httpx.Client",
                   return_value=mock_client):
            from probes.probe_tagger import _try_search_fallback
            ok, summary, warnings = _try_search_fallback()

        self.assertIn("X-RateLimit-Remaining",
                      summary.get("ratelimit_headers", {}),
                      "Expected ratelimit header captured in summary")
        self.assertNotIn("Content-Type",
                         summary.get("ratelimit_headers", {}),
                         "Non-ratelimit header should not be captured")


class TestScryfallSearchFixtureShape(unittest.TestCase):
    """Validate that the canned fixture matches the required Scryfall response shape."""

    def _load_fixture(self, name: str) -> dict:
        path = FIXTURES_DIR / name
        self.assertTrue(path.exists(), f"Fixture not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_fixture_has_required_keys(self):
        data = self._load_fixture("scryfall_search_otag_removal.json")
        for key in ("object", "total_cards", "has_more", "data"):
            self.assertIn(key, data, f"Fixture missing required key: {key}")

    def test_fixture_data_items_have_oracle_id(self):
        data = self._load_fixture("scryfall_search_otag_removal.json")
        for card in data["data"]:
            self.assertIn("oracle_id", card,
                          "Each card in fixture must have oracle_id")
            self.assertIn("name", card)
            self.assertIn("type_line", card)

    def test_fixture_total_cards_matches_data_length(self):
        data = self._load_fixture("scryfall_search_otag_removal.json")
        self.assertEqual(data["total_cards"], len(data["data"]),
                         "Fixture total_cards must match len(data) for single-page fixture")

    def test_fixture_tag_list_has_expected_structure(self):
        data = self._load_fixture("tag_list_response.json")
        self.assertIn("tags", data)
        for tag in data["tags"]:
            self.assertIn("name", tag)
            self.assertIn("type", tag)
            self.assertIn("card_count", tag)


class TestTryGraphQL(unittest.TestCase):
    """Unit tests for _try_graphql helper."""

    def test_auth_error_returns_warning_not_exception(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with patch("probes.probe_tagger.httpx.post", return_value=mock_resp):
            from probes.probe_tagger import _try_graphql
            status, warnings = _try_graphql(0.0)

        self.assertEqual(status, "403_auth_required")
        self.assertTrue(any("403" in w for w in warnings))

    def test_network_error_returns_unreachable(self):
        with patch("probes.probe_tagger.httpx.post",
                   side_effect=Exception("timed out")):
            from probes.probe_tagger import _try_graphql
            status, warnings = _try_graphql(0.0)

        self.assertEqual(status, "unreachable")
        self.assertTrue(any("timed out" in w for w in warnings))

    def test_graphql_ok_when_schema_returned(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {"__schema": {"queryType": {"name": "Query"}}}
        }

        with patch("probes.probe_tagger.httpx.post", return_value=mock_resp):
            from probes.probe_tagger import _try_graphql
            status, warnings = _try_graphql(0.0)

        # _try_graphql itself returns "graphql_ok" with no warnings;
        # the "consider using it directly" note is added by the outer probe().
        self.assertEqual(status, "graphql_ok")
        # No errors — list may be empty or contain debug notes, but no failures
        self.assertFalse(any("error" in w.lower() for w in warnings))


if __name__ == "__main__":
    unittest.main()
