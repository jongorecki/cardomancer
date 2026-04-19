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


if __name__ == "__main__":
    unittest.main()
