# tests/enrichment/test_edhrec.py
# Unit tests for EDHRECSource.
#
# History:
#   2026-04-23 — Rewrote archetype tests for new page-union approach.
#                Removed ARCHETYPES catalogue tests (catalogue deleted).

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import enrichment_db
from web_enrichment.edhrec import (
    EDHRECSource,
    COLOR_SLUGS,
    ARCHETYPE_MIN_PAGES,
    MAX_CARDS_PER_COLOR_PAGE,
    MAX_CARDS_PER_THEME_PAGE,
    _slug,
    _lookup,
    _extract_cardlists,
)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

class TestEDHRECHelpers(unittest.TestCase):

    def test_slug_basic(self):
        self.assertEqual(_slug("Sol Ring"), "sol-ring")
        self.assertEqual(_slug("Atraxa, Praetors' Voice"), "atraxa-praetors-voice")
        self.assertEqual(_slug("Thassa's Oracle"), "thassas-oracle")
        self.assertEqual(_slug("Edgar Markov"), "edgar-markov")

    def test_lookup_hit(self):
        name_map = {"Sol Ring": "oid-abc"}
        self.assertEqual(_lookup("Sol Ring", name_map), "oid-abc")

    def test_lookup_miss(self):
        self.assertIsNone(_lookup("Unknown Card", {}))

    def test_extract_cardlists_empty(self):
        self.assertEqual(_extract_cardlists({}), [])
        self.assertEqual(_extract_cardlists({"container": {}}), [])

    def test_extract_cardlists_found(self):
        data = {"container": {"json_dict": {"cardlists": [{"header": "Top"}]}}}
        self.assertEqual(len(_extract_cardlists(data)), 1)


# ---------------------------------------------------------------------------
# COLOR_SLUGS constant
# ---------------------------------------------------------------------------

class TestColorSlugs(unittest.TestCase):

    def test_count(self):
        # 5 mono + 10 guild + 10 shard/wedge + 5 four-color + 1 wubrg + 1 colorless
        self.assertEqual(len(COLOR_SLUGS), 32)

    def test_mono_colors_present(self):
        for c in ("white", "blue", "black", "red", "green"):
            self.assertIn(c, COLOR_SLUGS, f"Missing mono-color: {c}")

    def test_guilds_present(self):
        for g in ("azorius", "dimir", "rakdos", "gruul", "selesnya",
                  "orzhov", "izzet", "golgari", "boros", "simic"):
            self.assertIn(g, COLOR_SLUGS, f"Missing guild: {g}")

    def test_shards_wedges_present(self):
        for s in ("esper", "grixis", "jund", "naya", "bant",
                  "abzan", "jeskai", "sultai", "mardu", "temur"):
            self.assertIn(s, COLOR_SLUGS, f"Missing shard/wedge: {s}")

    def test_four_color_present(self):
        for f in ("sans-white", "sans-blue", "sans-black", "sans-red", "sans-green"):
            self.assertIn(f, COLOR_SLUGS, f"Missing four-color: {f}")

    def test_wubrg_and_colorless_present(self):
        self.assertIn("wubrg", COLOR_SLUGS)
        self.assertIn("colorless", COLOR_SLUGS)

    def test_no_duplicates(self):
        self.assertEqual(len(COLOR_SLUGS), len(set(COLOR_SLUGS)))


# ---------------------------------------------------------------------------
# _compute_enrichment (new page-union logic)
# ---------------------------------------------------------------------------

class TestEDHRECComputationPageUnion(unittest.TestCase):

    def setUp(self):
        self.source = EDHRECSource()

    def _make_name_map(self) -> dict[str, str]:
        return {
            "Sol Ring": "oid-sol-ring",
            "Counterspell": "oid-counterspell",
            "Birds of Paradise": "oid-bop",
            "Grizzly Bears": "oid-bears",
            "Atraxa, Praetors' Voice": "oid-atraxa",
        }

    def _make_page(self, names: list[str]) -> list[dict]:
        return [{"name": n, "num_decks": 100, "potential_decks": 1000}
                for n in names]

    # --- Universal staple tests (unchanged logic) ---

    def _compute(self, top_cards=None, archetype_pages=None, card_details=None,
                 name_map=None, warnings=None, page_slug_map=None,
                 top_commander_cards=None):
        """Convenience wrapper that provides required new args with defaults."""
        return self.source._compute_enrichment(
            top_cards=top_cards or [],
            archetype_pages=archetype_pages or [],
            page_slug_map=page_slug_map or {},
            top_commander_cards=top_commander_cards or [],
            card_details=card_details or {},
            name_map=name_map if name_map is not None else self._make_name_map(),
            warnings=warnings if warnings is not None else [],
        )

    def test_universal_staple_from_top_cards(self):
        top_cards = [
            {"name": "Sol Ring", "num_decks": 8000000,
             "potential_decks": 9000000},
        ]
        staple_rows, salt_rows, _, _ = self._compute(top_cards=top_cards)
        oids = [r["oracle_id"] for r in staple_rows if r["tier"] == "universal"]
        self.assertIn("oid-sol-ring", oids)

    def test_below_threshold_not_universal(self):
        top_cards = [
            {"name": "Grizzly Bears",
             "num_decks": 100, "potential_decks": 9000000},
        ]
        staple_rows, _, _, _ = self._compute(top_cards=top_cards)
        oids = [r["oracle_id"] for r in staple_rows if r["tier"] == "universal"]
        self.assertNotIn("oid-bears", oids)

    def test_salt_from_card_page(self):
        card_details = {
            "expropriate": {
                "name": "Expropriate",
                "num_decks": 100000,
                "potential_decks": 9000000,
                "salt": 4.2,
            }
        }
        name_map = {"Expropriate": "oid-exprop", **self._make_name_map()}
        _, salt_rows, _, _ = self._compute(
            card_details=card_details, name_map=name_map)
        salts = {r["oracle_id"]: r["salt"] for r in salt_rows}
        self.assertAlmostEqual(salts.get("oid-exprop"), 4.2, places=1)

    # --- Archetype page-union tests ---

    def test_archetype_staple_appears_in_enough_pages(self):
        # Sol Ring in top of 3 pages → qualifies
        pages = [
            self._make_page(["Sol Ring"]),
            self._make_page(["Sol Ring"]),
            self._make_page(["Sol Ring"]),
        ]
        staple_rows, _, _, _ = self._compute(archetype_pages=pages)
        archetype_oids = {r["oracle_id"] for r in staple_rows
                         if r["tier"] == "archetype"}
        self.assertIn("oid-sol-ring", archetype_oids)

    def test_archetype_not_enough_pages(self):
        # Birds of Paradise in only 2 pages → does not qualify (need >= 3)
        pages = [
            self._make_page(["Birds of Paradise"]),
            self._make_page(["Birds of Paradise"]),
        ]
        staple_rows, _, _, _ = self._compute(archetype_pages=pages)
        archetype_oids = {r["oracle_id"] for r in staple_rows
                         if r["tier"] == "archetype"}
        self.assertNotIn("oid-bop", archetype_oids)

    def test_archetype_page_count_is_min(self):
        # Exactly ARCHETYPE_MIN_PAGES → qualifies
        pages = [self._make_page(["Sol Ring"]) for _ in range(ARCHETYPE_MIN_PAGES)]
        staple_rows, _, _, _ = self._compute(archetype_pages=pages)
        archetype_oids = {r["oracle_id"] for r in staple_rows
                         if r["tier"] == "archetype"}
        self.assertIn("oid-sol-ring", archetype_oids)

    def test_archetype_page_count_one_below_min(self):
        # ARCHETYPE_MIN_PAGES - 1 → does NOT qualify
        pages = [self._make_page(["Sol Ring"])
                 for _ in range(ARCHETYPE_MIN_PAGES - 1)]
        staple_rows, _, _, _ = self._compute(archetype_pages=pages)
        archetype_oids = {r["oracle_id"] for r in staple_rows
                         if r["tier"] == "archetype"}
        self.assertNotIn("oid-sol-ring", archetype_oids)

    def test_dedup_within_single_page(self):
        # Duplicate entries within one page should count as 1 appearance
        page = self._make_page(["Sol Ring", "Sol Ring", "Sol Ring"])
        # Only 2 more pages (total 3) should trigger archetype
        pages = [page] + [self._make_page(["Sol Ring"])] * 2
        staple_rows, _, _, _ = self._compute(archetype_pages=pages)
        archetype_oids = {r["oracle_id"] for r in staple_rows
                         if r["tier"] == "archetype"}
        # Should appear in exactly 3 pages (deduped within each page)
        self.assertIn("oid-sol-ring", archetype_oids)

    def test_unknown_card_skipped(self):
        top_cards = [{"name": "Completely Unknown Card",
                      "num_decks": 9000000, "potential_decks": 9000001}]
        warnings: list[str] = []
        staple_rows, _, _, _ = self._compute(
            top_cards=top_cards, warnings=warnings)
        self.assertEqual(len(staple_rows), 0)

    def test_archetype_score_is_fraction_of_pages(self):
        # 3 appearances out of 10 pages → score = 0.3
        pages = [self._make_page(["Sol Ring"])] * 3 + [
            self._make_page(["Counterspell"])] * 7
        staple_rows, _, _, _ = self._compute(archetype_pages=pages)
        sol_ring_row = next(
            (r for r in staple_rows
             if r["tier"] == "archetype" and r["oracle_id"] == "oid-sol-ring"),
            None
        )
        self.assertIsNotNone(sol_ring_row)
        self.assertAlmostEqual(sol_ring_row["score"], 3 / 10, places=5)

    def test_archetypes_json_stores_page_count(self):
        # archetypes_json should be JSON-encoded count (int)
        pages = [self._make_page(["Sol Ring"])] * 5
        staple_rows, _, _, _ = self._compute(archetype_pages=pages)
        sol_row = next(
            r for r in staple_rows
            if r["tier"] == "archetype" and r["oracle_id"] == "oid-sol-ring"
        )
        self.assertEqual(json.loads(sol_row["archetypes_json"]), 5)

    def test_empty_pages_graceful(self):
        # Empty page list → no archetype rows, no crash
        staple_rows, _, _, _ = self._compute()
        archetype_oids = [r for r in staple_rows if r["tier"] == "archetype"]
        self.assertEqual(len(archetype_oids), 0)

    def test_empty_page_within_list_skipped(self):
        # Pages that have zero cardviews contribute nothing
        pages = [[], [], []]
        staple_rows, _, _, _ = self._compute(archetype_pages=pages)
        archetype_oids = [r for r in staple_rows if r["tier"] == "archetype"]
        self.assertEqual(len(archetype_oids), 0)

    def test_universal_and_archetype_can_coexist(self):
        # A card that is universal AND archetype gets two rows
        top_cards = [
            {"name": "Sol Ring", "num_decks": 8000000, "potential_decks": 9000000}
        ]
        pages = [self._make_page(["Sol Ring"])] * ARCHETYPE_MIN_PAGES
        staple_rows, _, _, _ = self._compute(
            top_cards=top_cards, archetype_pages=pages)
        tiers = {r["tier"] for r in staple_rows
                 if r["oracle_id"] == "oid-sol-ring"}
        self.assertIn("universal", tiers)
        self.assertIn("archetype", tiers)


# ---------------------------------------------------------------------------
# Theme index parsing
# ---------------------------------------------------------------------------

class TestThemeIndexParsing(unittest.TestCase):

    def setUp(self):
        self.source = EDHRECSource()
        self.source._client = MagicMock()

    def _mock_response(self, data: dict):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = data
        resp.raise_for_status = MagicMock()
        return resp

    def test_theme_index_slug_key(self):
        # Standard shape: {"slug": "...", "name": "..."}
        data = {
            "container": {
                "json_dict": {
                    "themes": [
                        {"slug": "lifegain", "name": "Lifegain"},
                        {"slug": "artifacts", "name": "Artifacts"},
                    ]
                }
            }
        }
        self.source._client.get.return_value = self._mock_response(data)
        slugs = self.source._fetch_theme_index([])
        self.assertEqual(slugs, ["lifegain", "artifacts"])

    def test_theme_index_sanitized_key_fallback(self):
        # Alternative shape using 'sanitized' instead of 'slug'
        data = {
            "container": {
                "json_dict": {
                    "themes": [
                        {"sanitized": "tokens", "name": "Tokens"},
                    ]
                }
            }
        }
        self.source._client.get.return_value = self._mock_response(data)
        slugs = self.source._fetch_theme_index([])
        self.assertEqual(slugs, ["tokens"])

    def test_theme_index_string_entries(self):
        # Shape where each entry is just a string slug
        data = {
            "container": {
                "json_dict": {
                    "themes": ["lifegain", "artifacts", "tokens"]
                }
            }
        }
        self.source._client.get.return_value = self._mock_response(data)
        slugs = self.source._fetch_theme_index([])
        self.assertEqual(slugs, ["lifegain", "artifacts", "tokens"])

    def test_theme_index_unavailable_returns_empty(self):
        # 403 → _get returns None
        resp = MagicMock()
        resp.status_code = 403
        self.source._client.get.return_value = resp
        warnings: list[str] = []
        slugs = self.source._fetch_theme_index(warnings)
        self.assertEqual(slugs, [])
        self.assertTrue(any("themes" in w for w in warnings))

    def test_theme_index_capped_at_max(self):
        from web_enrichment.edhrec import MAX_THEME_PAGES
        many_themes = [{"slug": f"theme-{i}"} for i in range(MAX_THEME_PAGES + 50)]
        data = {"container": {"json_dict": {"themes": many_themes}}}
        self.source._client.get.return_value = self._mock_response(data)
        slugs = self.source._fetch_theme_index([])
        self.assertEqual(len(slugs), MAX_THEME_PAGES)


# ---------------------------------------------------------------------------
# Fixture-based integration tests
# ---------------------------------------------------------------------------

class TestEDHRECFixtures(unittest.TestCase):
    """Validate the real fixture files."""

    FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "edhrec"

    def test_commander_fixture_shape(self):
        fpath = self.FIXTURE_DIR / "commander_atraxa.json"
        if not fpath.exists():
            self.skipTest("EDHREC commander fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        cardlists = _extract_cardlists(data)
        self.assertIsInstance(cardlists, list)
        self.assertGreater(len(cardlists), 0)
        cv = cardlists[0].get("cardviews", [])
        self.assertIsInstance(cv, list)
        if cv:
            self.assertIn("name", cv[0])

    def test_card_fixture_has_salt(self):
        fpath = self.FIXTURE_DIR / "card_sol_ring.json"
        if not fpath.exists():
            self.skipTest("EDHREC card fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        card = (data.get("container", {})
                .get("json_dict", {})
                .get("card", {}))
        self.assertIn("salt", card)
        self.assertIn("num_decks", card)
        self.assertIn("potential_decks", card)

    def test_color_page_fixture_shape(self):
        fpath = self.FIXTURE_DIR / "color_white.json"
        if not fpath.exists():
            self.skipTest("Color page fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        cardlists = _extract_cardlists(data)
        self.assertGreater(len(cardlists), 0)
        cv = cardlists[0].get("cardviews", [])
        self.assertGreater(len(cv), 0)
        self.assertIn("name", cv[0])

    def test_theme_page_fixture_shape(self):
        fpath = self.FIXTURE_DIR / "theme_lifegain.json"
        if not fpath.exists():
            self.skipTest("Theme page fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        cardlists = _extract_cardlists(data)
        self.assertGreater(len(cardlists), 0)
        cv = cardlists[0].get("cardviews", [])
        self.assertGreater(len(cv), 0)

    def test_themes_index_fixture_shape(self):
        fpath = self.FIXTURE_DIR / "themes_index.json"
        if not fpath.exists():
            self.skipTest("Themes index fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        themes = (data.get("container", {})
                  .get("json_dict", {})
                  .get("themes", []))
        self.assertGreater(len(themes), 0)
        self.assertIn("slug", themes[0])

    def test_top_commanders_fixture_shape(self):
        fpath = self.FIXTURE_DIR / "top_commanders.json"
        if not fpath.exists():
            self.skipTest("Top commanders fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        cardlists = _extract_cardlists(data)
        self.assertGreater(len(cardlists), 0)
        cv = cardlists[0].get("cardviews", [])
        self.assertGreater(len(cv), 0)


# ---------------------------------------------------------------------------
# DB write + idempotency
# ---------------------------------------------------------------------------

class TestEDHRECDBWrite(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        self.source = EDHRECSource()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get_conn(self):
        return enrichment_db.get_connection(db_path=self.db_path)

    def _write_rows(self, staple_rows, salt_rows,
                    theme_rows=None, commander_rank_rows=None):
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            conn = enrichment_db.get_connection()
            try:
                changed = self.source._write(
                    conn, staple_rows, salt_rows,
                    theme_rows or [], commander_rank_rows or [], []
                )
                return changed
            finally:
                conn.close()
        finally:
            enrichment_db.DB_PATH = orig

    def test_write_staple_rows(self):
        rows = [
            {"oracle_id": "oid-1", "tier": "archetype", "source": "edhrec",
             "score": 0.5, "archetypes_json": "3", "last_updated": "2026-01-01T00:00:00+00:00"}
        ]
        changed = self._write_rows(rows, [])
        self.assertEqual(changed, 1)
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM staples WHERE oracle_id='oid-1'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["tier"], "archetype")
        finally:
            conn.close()

    def test_idempotent_write(self):
        rows = [
            {"oracle_id": "oid-2", "tier": "universal", "source": "edhrec",
             "score": 0.9, "archetypes_json": None, "last_updated": "2026-01-01T00:00:00+00:00"}
        ]
        self._write_rows(rows, [])
        # Second write should succeed and overwrite
        rows[0]["score"] = 0.95
        self._write_rows(rows, [])
        conn = self._get_conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM staples WHERE oracle_id='oid-2'"
            ).fetchone()[0]
            self.assertEqual(count, 1)
            score = conn.execute(
                "SELECT score FROM staples WHERE oracle_id='oid-2'"
            ).fetchone()[0]
            self.assertAlmostEqual(score, 0.95, places=3)
        finally:
            conn.close()

    def test_no_partial_commit_on_bad_salt_row(self):
        # A malformed salt row (missing oracle_id) should rollback the transaction,
        # not partially write the staple rows.
        staple_rows = [
            {"oracle_id": "oid-3", "tier": "universal", "source": "edhrec",
             "score": 0.7, "archetypes_json": None, "last_updated": "2026-01-01"}
        ]
        bad_salt_rows = [
            # Missing oracle_id key → will cause a DB constraint error
            {"salt": 2.5, "last_updated": "2026-01-01"}
        ]
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            conn = enrichment_db.get_connection()
            try:
                with self.assertRaises(Exception):
                    self.source._write(conn, staple_rows, bad_salt_rows, [], [], [])
                # staple row should NOT be committed
                count = conn.execute(
                    "SELECT COUNT(*) FROM staples WHERE oracle_id='oid-3'"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.close()
        finally:
            enrichment_db.DB_PATH = orig


# ---------------------------------------------------------------------------
# Fetch-helper mocking
# ---------------------------------------------------------------------------

class TestEDHRECFetchHelpers(unittest.TestCase):

    def setUp(self):
        self.source = EDHRECSource()
        self.source._client = MagicMock()

    def _mock_ok(self, data):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = data
        resp.raise_for_status = MagicMock()
        return resp

    def _mock_403(self):
        resp = MagicMock()
        resp.status_code = 403
        return resp

    def _color_page_data(self, names):
        return {
            "container": {
                "json_dict": {
                    "cardlists": [
                        {"cardviews": [{"name": n} for n in names]}
                    ]
                }
            }
        }

    def test_fetch_color_pages_skips_403(self):
        self.source._client.get.return_value = self._mock_403()
        warnings: list[str] = []
        result = self.source._fetch_color_pages(None, warnings)
        # Should get one empty list per slug, not crash
        self.assertEqual(len(result), len(COLOR_SLUGS))
        # All pages should be empty
        for page in result:
            self.assertEqual(page, [])

    def test_fetch_color_pages_caps_at_max(self):
        names = [f"Card{i}" for i in range(MAX_CARDS_PER_COLOR_PAGE + 50)]
        self.source._client.get.return_value = self._mock_ok(
            self._color_page_data(names))
        result = self.source._fetch_color_pages(None, [])
        for page in result:
            self.assertLessEqual(len(page), MAX_CARDS_PER_COLOR_PAGE)

    def test_fetch_theme_pages_caps_at_max_per_theme(self):
        names = [f"Card{i}" for i in range(MAX_CARDS_PER_THEME_PAGE + 50)]
        self.source._client.get.return_value = self._mock_ok(
            self._color_page_data(names))
        result, slug_map = self.source._fetch_theme_pages(
            ["lifegain", "tokens"], None, [])
        for page in result:
            self.assertLessEqual(len(page), MAX_CARDS_PER_THEME_PAGE)

    def test_fetch_theme_pages_returns_slug_map(self):
        # Each theme page index should map to its slug
        self.source._client.get.return_value = self._mock_ok(
            self._color_page_data(["Sol Ring"]))
        pages, slug_map = self.source._fetch_theme_pages(
            ["lifegain", "tokens", "aristocrats"], None, [])
        self.assertEqual(len(pages), 3)
        self.assertEqual(slug_map[0], "lifegain")
        self.assertEqual(slug_map[1], "tokens")
        self.assertEqual(slug_map[2], "aristocrats")

    def test_fetch_theme_pages_skips_unavailable_but_maps_present(self):
        # A 403 response should produce an empty page but still not shift indices
        calls = [0]
        def side_effect(url, **kw):
            calls[0] += 1
            if calls[0] == 2:  # second page → 403
                return self._mock_403()
            return self._mock_ok(self._color_page_data(["Sol Ring"]))
        self.source._client.get.side_effect = side_effect
        warnings: list[str] = []
        pages, slug_map = self.source._fetch_theme_pages(
            ["lifegain", "tokens", "aristocrats"], None, warnings)
        self.assertEqual(len(pages), 3)
        # Index 0 = lifegain (ok), index 2 = aristocrats (ok)
        # index 1 = tokens (403 → empty, not in slug_map)
        self.assertIn(0, slug_map)
        self.assertNotIn(1, slug_map)  # 403 page excluded
        self.assertIn(2, slug_map)

    def test_fetch_top_commanders_page_uses_fallback(self):
        # First call (top-commanders/year.json) returns 403, second succeeds
        calls = [0]
        def side_effect(url, **kw):
            calls[0] += 1
            if calls[0] == 1:
                return self._mock_403()
            return self._mock_ok(self._color_page_data(["Atraxa, Praetors' Voice"]))
        self.source._client.get.side_effect = side_effect
        result = self.source._fetch_top_commanders_page([])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Atraxa, Praetors' Voice")

    def test_fetch_top_year_warns_on_missing_cardlists(self):
        self.source._client.get.return_value = self._mock_ok(
            {"container": {"json_dict": {"cardlists": []}}})
        warnings: list[str] = []
        result = self.source._fetch_top_year(warnings)
        self.assertEqual(result, [])
        self.assertTrue(any("cardlists" in w for w in warnings))


# ---------------------------------------------------------------------------
# Emit progress events
# ---------------------------------------------------------------------------

class TestEDHRECEmit(unittest.TestCase):

    def test_emit_calls_callback(self):
        events = []
        def callback(event, data):
            events.append((event, data))
        EDHRECSource._emit(callback, step=1, total=5, message="test msg")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "enrichment_refresh_progress")
        d = events[0][1]
        self.assertEqual(d["step"], 1)
        self.assertEqual(d["total"], 5)
        self.assertEqual(d["message"], "test msg")
        self.assertEqual(d["source"], "edhrec")

    def test_emit_none_is_noop(self):
        # Should not raise
        EDHRECSource._emit(None, 0, 5, "noop")


# ---------------------------------------------------------------------------
# Removed: TestArchetypeCatalogue — ARCHETYPES dict and
# _build_commander_archetype_map() deleted in 2026-04-23 refactor.
# The page-union approach replaces the static catalogue entirely.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 1.10: Theme membership computation (new)
# ---------------------------------------------------------------------------

class TestEDHRECThemes(unittest.TestCase):
    """Theme rows computed from page_slug_map + archetype_pages."""

    def setUp(self):
        self.source = EDHRECSource()
        self.name_map = {
            "Sol Ring": "oid-sol-ring",
            "Cultivate": "oid-cultivate",
            "Counterspell": "oid-counterspell",
        }

    def _compute(self, archetype_pages, page_slug_map, **kw):
        return self.source._compute_enrichment(
            top_cards=[],
            archetype_pages=archetype_pages,
            page_slug_map=page_slug_map,
            top_commander_cards=[],
            card_details={},
            name_map=self.name_map,
            warnings=[],
            **kw,
        )

    def test_theme_rows_generated_for_theme_page(self):
        # Page 0 is a color page (no slug), page 1 is "lifegain" theme
        pages = [
            [{"name": "Sol Ring", "num_decks": 100, "potential_decks": 1000}],
            [{"name": "Cultivate", "num_decks": 200, "potential_decks": 1000}],
        ]
        slug_map = {1: "lifegain"}
        _, _, theme_rows, _ = self._compute(pages, slug_map)
        self.assertTrue(any(
            r["oracle_id"] == "oid-cultivate" and r["theme_name"] == "lifegain"
            for r in theme_rows
        ))

    def test_no_theme_rows_for_color_pages(self):
        # Color pages have no slug in the map → no theme rows
        pages = [
            [{"name": "Sol Ring", "num_decks": 100, "potential_decks": 1000}],
        ]
        _, _, theme_rows, _ = self._compute(pages, {})
        self.assertEqual(theme_rows, [])

    def test_card_in_multiple_themes(self):
        # Sol Ring appears in both "artifacts" and "tokens" pages
        pages = [
            [{"name": "Sol Ring", "num_decks": 100, "potential_decks": 1000}],
            [{"name": "Sol Ring", "num_decks": 100, "potential_decks": 1000}],
        ]
        slug_map = {0: "artifacts", 1: "tokens"}
        _, _, theme_rows, _ = self._compute(pages, slug_map)
        themes_for_sol = {r["theme_name"] for r in theme_rows
                         if r["oracle_id"] == "oid-sol-ring"}
        self.assertIn("artifacts", themes_for_sol)
        self.assertIn("tokens", themes_for_sol)

    def test_theme_membership_source_is_edhrec(self):
        pages = [
            [{"name": "Cultivate", "num_decks": 100, "potential_decks": 1000}],
        ]
        _, _, theme_rows, _ = self._compute(pages, {0: "ramp"})
        for r in theme_rows:
            self.assertEqual(r["source"], "edhrec")

    def test_dedup_within_page_does_not_create_duplicate_theme_row(self):
        # A card appearing twice on the same page should only create one theme row
        pages = [
            [
                {"name": "Sol Ring", "num_decks": 100, "potential_decks": 1000},
                {"name": "Sol Ring", "num_decks": 100, "potential_decks": 1000},
            ]
        ]
        _, _, theme_rows, _ = self._compute(pages, {0: "artifacts"})
        sol_ring_rows = [r for r in theme_rows
                        if r["oracle_id"] == "oid-sol-ring"
                        and r["theme_name"] == "artifacts"]
        self.assertEqual(len(sol_ring_rows), 1)

    def test_theme_write_idempotent(self):
        """Writing theme rows twice → same single row in DB (ON CONFLICT update)."""
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "test.db")
        try:
            orig = enrichment_db.DB_PATH
            enrichment_db.DB_PATH = db_path
            try:
                theme_rows = [
                    {"oracle_id": "oid-x", "theme_name": "artifacts",
                     "source": "edhrec", "inclusion_pct": None},
                ]
                conn = enrichment_db.get_connection()
                try:
                    self.source._write(conn, [], [], theme_rows, [], [])
                    self.source._write(conn, [], [], theme_rows, [], [])
                    count = conn.execute(
                        "SELECT COUNT(*) FROM themes WHERE oracle_id='oid-x'"
                    ).fetchone()[0]
                    self.assertEqual(count, 1)
                finally:
                    conn.close()
            finally:
                enrichment_db.DB_PATH = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_theme_write_persists_to_db(self):
        """Theme rows written by _write() are readable from the DB."""
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "test.db")
        try:
            orig = enrichment_db.DB_PATH
            enrichment_db.DB_PATH = db_path
            try:
                theme_rows = [
                    {"oracle_id": "oid-y", "theme_name": "lifegain",
                     "source": "edhrec", "inclusion_pct": None},
                ]
                conn = enrichment_db.get_connection()
                try:
                    self.source._write(conn, [], [], theme_rows, [], [])
                    row = conn.execute(
                        "SELECT * FROM themes WHERE oracle_id='oid-y'"
                    ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["theme_name"], "lifegain")
                    self.assertEqual(row["source"], "edhrec")
                finally:
                    conn.close()
            finally:
                enrichment_db.DB_PATH = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Phase 1.10: Commander popularity computation (new)
# ---------------------------------------------------------------------------

class TestEDHRECCommanderRanks(unittest.TestCase):
    """Commander rank rows from top_commander_cards."""

    def setUp(self):
        self.source = EDHRECSource()
        self.name_map = {
            "Atraxa, Praetors' Voice": "oid-atraxa",
            "Edgar Markov": "oid-edgar",
            "Sol Ring": "oid-sol-ring",
        }

    def _compute_commander_rows(self, top_commander_cards, **kw):
        _, _, _, cmd_rows = self.source._compute_enrichment(
            top_cards=[],
            archetype_pages=[],
            page_slug_map={},
            top_commander_cards=top_commander_cards,
            card_details={},
            name_map=self.name_map,
            warnings=[],
            **kw,
        )
        return cmd_rows

    def test_commander_rank_from_num_decks(self):
        top_commanders = [
            {"name": "Atraxa, Praetors' Voice", "num_decks": 80000,
             "potential_decks": 100000},
        ]
        rows = self._compute_commander_rows(top_commanders)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["oracle_id"], "oid-atraxa")
        self.assertEqual(rows[0]["deck_count"], 80000)

    def test_commander_rank_uses_deck_count_key_fallback(self):
        # Some EDHREC pages may use "deck_count" instead of "num_decks"
        top_commanders = [
            {"name": "Edgar Markov", "deck_count": 60000},
        ]
        rows = self._compute_commander_rows(top_commanders)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deck_count"], 60000)

    def test_commander_with_zero_decks_excluded(self):
        top_commanders = [
            {"name": "Atraxa, Praetors' Voice", "num_decks": 0},
        ]
        rows = self._compute_commander_rows(top_commanders)
        self.assertEqual(len(rows), 0)

    def test_unknown_commander_skipped(self):
        top_commanders = [
            {"name": "Unknown Commander", "num_decks": 50000},
        ]
        rows = self._compute_commander_rows(top_commanders)
        self.assertEqual(len(rows), 0)

    def test_dedup_commanders(self):
        # Same commander appearing twice → only one row
        top_commanders = [
            {"name": "Atraxa, Praetors' Voice", "num_decks": 80000},
            {"name": "Atraxa, Praetors' Voice", "num_decks": 50000},
        ]
        rows = self._compute_commander_rows(top_commanders)
        atraxa_rows = [r for r in rows if r["oracle_id"] == "oid-atraxa"]
        self.assertEqual(len(atraxa_rows), 1)
        # First occurrence wins
        self.assertEqual(atraxa_rows[0]["deck_count"], 80000)

    def test_commander_source_is_edhrec(self):
        top_commanders = [
            {"name": "Atraxa, Praetors' Voice", "num_decks": 80000},
        ]
        rows = self._compute_commander_rows(top_commanders)
        self.assertEqual(rows[0]["source"], "edhrec")

    def test_commander_rank_written_to_db(self):
        """commander_ranks table is populated by _write()."""
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "test.db")
        try:
            orig = enrichment_db.DB_PATH
            enrichment_db.DB_PATH = db_path
            try:
                cmd_rows = [
                    {"oracle_id": "oid-atraxa", "deck_count": 80000,
                     "avg_synergy_json": None, "source": "edhrec",
                     "last_updated": "2026-01-01T00:00:00+00:00"},
                ]
                conn = enrichment_db.get_connection()
                try:
                    self.source._write(conn, [], [], [], cmd_rows, [])
                    row = conn.execute(
                        "SELECT * FROM commander_ranks WHERE oracle_id='oid-atraxa'"
                    ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["deck_count"], 80000)
                    self.assertEqual(row["source"], "edhrec")
                finally:
                    conn.close()
            finally:
                enrichment_db.DB_PATH = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_commander_rank_idempotent(self):
        """Writing commander_rank twice → single row (ON CONFLICT update)."""
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "test.db")
        try:
            orig = enrichment_db.DB_PATH
            enrichment_db.DB_PATH = db_path
            try:
                cmd_rows = [
                    {"oracle_id": "oid-edgar", "deck_count": 60000,
                     "avg_synergy_json": None, "source": "edhrec",
                     "last_updated": "2026-01-01T00:00:00+00:00"},
                ]
                conn = enrichment_db.get_connection()
                try:
                    self.source._write(conn, [], [], [], cmd_rows, [])
                    cmd_rows[0]["deck_count"] = 65000
                    self.source._write(conn, [], [], [], cmd_rows, [])
                    count = conn.execute(
                        "SELECT COUNT(*) FROM commander_ranks "
                        "WHERE oracle_id='oid-edgar'"
                    ).fetchone()[0]
                    self.assertEqual(count, 1)
                    updated = conn.execute(
                        "SELECT deck_count FROM commander_ranks "
                        "WHERE oracle_id='oid-edgar'"
                    ).fetchone()[0]
                    self.assertEqual(updated, 65000)
                finally:
                    conn.close()
            finally:
                enrichment_db.DB_PATH = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Phase 1.10: Repo query methods (salt, staples by theme, commander popularity)
# ---------------------------------------------------------------------------

class TestEDHRECRepoQueries(unittest.TestCase):
    """Test new EnrichmentRepo query methods that consume edhrec data."""

    def setUp(self):
        import tempfile
        import shutil
        from web_enrichment.repo import EnrichmentRepo
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        self.conn = enrichment_db.get_connection(db_path=self.db_path)
        self.repo = EnrichmentRepo(db_path=self.db_path)
        # Seed card universe
        enrichment_db.seed_card_universe(
            self.conn, [
                ("oid-sol-ring", "Sol Ring"),
                ("oid-atraxa", "Atraxa, Praetors' Voice"),
                ("oid-exprop", "Expropriate"),
            ]
        )

    def tearDown(self):
        import shutil
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_salt_score_returns_value(self):
        self.conn.execute(
            "INSERT INTO salt_scores (oracle_id, salt, last_updated) "
            "VALUES (?, ?, ?)", ("oid-exprop", 4.2, "2026-01-01")
        )
        self.conn.commit()
        salt = self.repo.get_salt_score("oid-exprop")
        self.assertIsNotNone(salt)
        self.assertAlmostEqual(salt, 4.2, places=1)

    def test_get_salt_score_none_for_unknown(self):
        self.assertIsNone(self.repo.get_salt_score("oid-unknown"))

    def test_get_commander_popularity_returns_deck_count(self):
        self.conn.execute(
            "INSERT INTO commander_ranks "
            "(oracle_id, deck_count, avg_synergy_json, source, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("oid-atraxa", 80000, None, "edhrec", "2026-01-01")
        )
        self.conn.commit()
        deck_count = self.repo.get_commander_popularity("oid-atraxa")
        self.assertEqual(deck_count, 80000)

    def test_get_commander_popularity_none_for_unknown(self):
        self.assertIsNone(self.repo.get_commander_popularity("oid-unknown"))

    def test_get_staples_by_theme_returns_oracle_ids(self):
        self.conn.execute(
            "INSERT INTO themes (oracle_id, theme_name, source, inclusion_pct) "
            "VALUES (?, ?, ?, ?)",
            ("oid-sol-ring", "artifacts", "edhrec", None)
        )
        self.conn.execute(
            "INSERT INTO themes (oracle_id, theme_name, source, inclusion_pct) "
            "VALUES (?, ?, ?, ?)",
            ("oid-atraxa", "artifacts", "edhrec", None)
        )
        self.conn.execute(
            "INSERT INTO themes (oracle_id, theme_name, source, inclusion_pct) "
            "VALUES (?, ?, ?, ?)",
            ("oid-exprop", "ramp", "edhrec", None)
        )
        self.conn.commit()
        artifacts = self.repo.get_staples_by_theme("artifacts")
        self.assertIn("oid-sol-ring", artifacts)
        self.assertIn("oid-atraxa", artifacts)
        self.assertNotIn("oid-exprop", artifacts)

    def test_get_staples_by_theme_empty_for_unknown_theme(self):
        result = self.repo.get_staples_by_theme("nonexistent-theme")
        self.assertEqual(result, [])

    def test_get_staples_by_color_returns_staples_for_color_slug(self):
        # Staples tagged with archetypes_json containing color slug "white"
        self.conn.execute(
            "INSERT INTO staples "
            "(oracle_id, tier, source, score, archetypes_json, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("oid-sol-ring", "archetype", "edhrec", 0.9,
             '["white", "blue"]', "2026-01-01")
        )
        self.conn.commit()
        result = self.repo.get_staples_by_color_or_theme("white")
        self.assertIn("oid-sol-ring", result)

    def test_get_staples_by_color_or_theme_combined(self):
        # Cards from both staples (by archetypes_json) and themes table
        self.conn.execute(
            "INSERT INTO themes (oracle_id, theme_name, source, inclusion_pct) "
            "VALUES (?, ?, ?, ?)",
            ("oid-atraxa", "artifacts", "edhrec", None)
        )
        self.conn.commit()
        result = self.repo.get_staples_by_color_or_theme("artifacts")
        self.assertIn("oid-atraxa", result)


if __name__ == "__main__":
    unittest.main()
