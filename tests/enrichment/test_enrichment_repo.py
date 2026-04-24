# tests/enrichment/test_enrichment_repo.py
# ---------------------------------------------------------------------------
# Phase 0A read-only repo: lookups, freshness, coverage overview.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import enrichment_db
from web_enrichment.repo import EnrichmentRepo


class TestEnrichmentRepo(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="repo_test_")
        self.db_path = os.path.join(self.tmpdir, "enrichment.db")
        self.conn = enrichment_db.get_connection(db_path=self.db_path)
        self.repo = EnrichmentRepo(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_card_returns_none_for_unknown(self):
        self.assertIsNone(self.repo.get_card("missing"))

    def test_get_card_minimal(self):
        enrichment_db.seed_card_universe(
            self.conn, [("oid-x", "Test Card")])
        self.conn.execute(
            "INSERT INTO tags (oracle_id, tag_name, source) "
            "VALUES (?, ?, ?)",
            ("oid-x", "removal", "scryfall_search"),
        )
        self.conn.execute(
            "INSERT INTO staples (oracle_id, tier, source, score, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("oid-x", "universal", "edhrec", 0.8, "now"),
        )
        self.conn.execute(
            "INSERT INTO salt_scores (oracle_id, salt, last_updated) "
            "VALUES (?, ?, ?)",
            ("oid-x", 2.5, "now"),
        )
        self.conn.commit()

        card = self.repo.get_card("oid-x")
        self.assertIsNotNone(card)
        self.assertEqual(card.oracle_id, "oid-x")
        self.assertIn("removal", card.tags)
        self.assertTrue(card.staples["universal"])
        self.assertFalse(card.staples["cedh"])
        self.assertEqual(card.salt, 2.5)

    def test_get_card_merges_local_tags(self):
        enrichment_db.seed_card_universe(self.conn, [("oid-y", "Card Y")])
        self.conn.execute(
            "INSERT INTO tags (oracle_id, tag_name, source) "
            "VALUES (?, ?, ?)",
            ("oid-y", "ramp", "scryfall_search"),
        )
        self.conn.execute(
            "INSERT INTO local_tags (oracle_id, tag_name, created_at) "
            "VALUES (?, ?, ?)",
            ("oid-y", "custom-flag", "now"),
        )
        self.conn.commit()

        tags = self.repo.get_card("oid-y").tags
        self.assertIn("ramp", tags)
        self.assertIn("custom-flag", tags)

    def test_archetype_staple_populates_archetypes_list(self):
        enrichment_db.seed_card_universe(self.conn, [("oid-z", "Card Z")])
        self.conn.execute(
            "INSERT INTO staples "
            "(oracle_id, tier, source, score, archetypes_json, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("oid-z", "archetype", "edhrec", 0.5,
             json.dumps(["lifegain", "tokens", "aristocrats"]), "now"),
        )
        self.conn.commit()

        card = self.repo.get_card("oid-z")
        self.assertTrue(card.staples["archetype"])
        self.assertEqual(card.staples["archetype_count"], 3)
        self.assertIn("tokens", card.staples["archetypes"])

    def test_coverage_overview_shape(self):
        enrichment_db.record_sync_attempt(
            self.conn, "tagger", success=True, coverage_pct=50.0)
        overview = self.repo.coverage_overview()
        self.assertIn("tagger", overview)
        self.assertAlmostEqual(overview["tagger"]["coverage_pct"], 50.0)

    def test_query_stub_returns_empty(self):
        cards, total = self.repo.query("otag:removal")
        self.assertEqual(cards, [])
        self.assertEqual(total, 0)

    def test_freshness_fresh_after_success(self):
        enrichment_db.record_sync_attempt(
            self.conn, "spellbook", success=True)
        enrichment_db.seed_card_universe(
            self.conn, [("oid-f", "Fresh Card")])
        card = self.repo.get_card("oid-f")
        self.assertEqual(card.source_freshness.get("spellbook"), "fresh")

    def test_freshness_stale_days(self):
        # Backdate last_success by 40 days.
        past = (datetime.now(timezone.utc) - timedelta(days=40))
        past_iso = past.isoformat(timespec="seconds")
        enrichment_db.record_sync_attempt(
            self.conn, "tagger", success=True)
        self.conn.execute(
            "UPDATE sync_metadata SET last_success = ? WHERE source = ?",
            (past_iso, "tagger"),
        )
        self.conn.commit()

        enrichment_db.seed_card_universe(
            self.conn, [("oid-s", "Stale Card")])
        card = self.repo.get_card("oid-s")
        self.assertTrue(
            card.source_freshness.get("tagger", "").startswith("stale_"))


class TestGetCEDHStaples(unittest.TestCase):
    """Round-trip tests for EnrichmentRepo.get_cedh_staples()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cedh_repo_test_")
        self.db_path = os.path.join(self.tmpdir, "enrichment.db")
        self.conn = enrichment_db.get_connection(db_path=self.db_path)
        self.repo = EnrichmentRepo(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_cedh_row(self, oracle_id: str, score: float,
                         last_updated: str = "2026-01-01T00:00:00") -> None:
        self.conn.execute(
            """INSERT INTO staples
               (oracle_id, tier, source, score, archetypes_json, last_updated)
               VALUES (?, 'cedh', 'edhtop16', ?, NULL, ?)""",
            (oracle_id, score, last_updated),
        )
        self.conn.commit()

    def test_returns_empty_when_no_data(self):
        result = self.repo.get_cedh_staples()
        self.assertEqual(result, [])

    def test_returns_all_cedh_rows(self):
        self._insert_cedh_row("oid-a", 0.80)
        self._insert_cedh_row("oid-b", 0.50)
        result = self.repo.get_cedh_staples()
        self.assertEqual(len(result), 2)

    def test_result_shape(self):
        self._insert_cedh_row("thassas-oracle", 0.54, "2026-04-24T00:00:00")
        result = self.repo.get_cedh_staples()
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertIn("oracle_id", row)
        self.assertIn("play_rate", row)
        self.assertIn("tournament_appearances", row)
        self.assertIn("last_refreshed", row)
        self.assertEqual(row["oracle_id"], "thassas-oracle")
        self.assertAlmostEqual(row["play_rate"], 0.54, places=5)
        self.assertIsNone(row["tournament_appearances"])
        self.assertEqual(row["last_refreshed"], "2026-04-24T00:00:00")

    def test_min_play_rate_filters(self):
        self._insert_cedh_row("above", 0.40)
        self._insert_cedh_row("below", 0.10)
        result = self.repo.get_cedh_staples(min_play_rate=0.20)
        oracle_ids = [r["oracle_id"] for r in result]
        self.assertIn("above", oracle_ids)
        self.assertNotIn("below", oracle_ids)

    def test_min_play_rate_zero_returns_all(self):
        self._insert_cedh_row("a", 0.80)
        self._insert_cedh_row("b", 0.01)
        result = self.repo.get_cedh_staples(min_play_rate=0.0)
        self.assertEqual(len(result), 2)

    def test_min_play_rate_exact_boundary_included(self):
        # A card at exactly the threshold should be included
        self._insert_cedh_row("boundary", 0.15)
        result = self.repo.get_cedh_staples(min_play_rate=0.15)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["oracle_id"], "boundary")

    def test_results_ordered_by_play_rate_descending(self):
        self._insert_cedh_row("low", 0.20)
        self._insert_cedh_row("high", 0.90)
        self._insert_cedh_row("mid", 0.50)
        result = self.repo.get_cedh_staples()
        rates = [r["play_rate"] for r in result]
        self.assertEqual(rates, sorted(rates, reverse=True))

    def test_only_edhtop16_source_returned(self):
        # Insert an edhrec cedh row — should NOT appear in get_cedh_staples
        self.conn.execute(
            """INSERT INTO staples
               (oracle_id, tier, source, score, archetypes_json, last_updated)
               VALUES ('edhrec-oid', 'cedh', 'edhrec', 0.9, NULL, '2026-01-01')"""
        )
        self._insert_cedh_row("edhtop16-oid", 0.9)
        self.conn.commit()

        result = self.repo.get_cedh_staples()
        oracle_ids = [r["oracle_id"] for r in result]
        self.assertIn("edhtop16-oid", oracle_ids)
        self.assertNotIn("edhrec-oid", oracle_ids)

    def test_non_cedh_tier_excluded(self):
        # Insert a universal staple from edhtop16 — should not appear
        self.conn.execute(
            """INSERT INTO staples
               (oracle_id, tier, source, score, archetypes_json, last_updated)
               VALUES ('universal-oid', 'universal', 'edhtop16', 0.9, NULL, '2026-01-01')"""
        )
        self._insert_cedh_row("cedh-oid", 0.9)
        self.conn.commit()

        result = self.repo.get_cedh_staples()
        oracle_ids = [r["oracle_id"] for r in result]
        self.assertIn("cedh-oid", oracle_ids)
        self.assertNotIn("universal-oid", oracle_ids)

    def test_idempotent_read(self):
        """Calling twice returns identical results."""
        self._insert_cedh_row("oid-1", 0.80)
        self._insert_cedh_row("oid-2", 0.30)
        first = self.repo.get_cedh_staples()
        second = self.repo.get_cedh_staples()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
