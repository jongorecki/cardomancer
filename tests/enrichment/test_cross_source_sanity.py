# tests/enrichment/test_cross_source_sanity.py
# Cross-source sanity checks. Run against a populated enrichment.db.
# These tests set up their own minimal fixture data rather than hitting
# live sources; they verify that the repository layer reads data correctly.

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import enrichment_db
from web_enrichment.repo import EnrichmentRepo

# Hardcoded oracle IDs for well-known cards (from Scryfall bulk 2026-04).
SOL_RING_OID   = "6ad8011d-3471-4369-9d68-b264cc027487"
GRIZZLY_OID    = "f6e4b1d2-e15d-4bca-a17b-e1b44b3d8e2f"  # approximate
THASSA_OID     = "thassas-oracle-oid"  # use synthetic for fixture test


def _seed_universe(conn, cards: list[tuple[str, str]]) -> None:
    enrichment_db.seed_card_universe(conn, cards)


class TestCrossSourceSanity(unittest.TestCase):
    """Verify that repo reads multi-source enrichment correctly."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test_cross.db")
        self.conn = enrichment_db.get_connection(db_path=self.db_path)
        self.repo = EnrichmentRepo(db_path=self.db_path)

        # Seed card_universe
        _seed_universe(self.conn, [
            (SOL_RING_OID, "Sol Ring"),
            (THASSA_OID, "Thassa's Oracle"),
        ])

        # Seed staples for Sol Ring (universal from EDHREC + cedh from edhtop16)
        ts = "2026-04-20T00:00:00"
        self.conn.executemany(
            """INSERT INTO staples (oracle_id, tier, source, score, last_updated)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (SOL_RING_OID, "universal", "edhrec",  0.84, ts),
                (SOL_RING_OID, "cedh",      "edhtop16", 0.48, ts),
                (THASSA_OID,   "cedh",      "edhtop16", 0.85, ts),
            ]
        )

        # Seed salt for Sol Ring
        self.conn.execute(
            "INSERT INTO salt_scores (oracle_id, salt, last_updated) "
            "VALUES (?, ?, ?)",
            (SOL_RING_OID, 1.46, ts)
        )

        # Seed combo for Thassa's Oracle
        self.conn.execute(
            "INSERT INTO combos (combo_id, result, identity, mana_needed, "
            "prerequisites_json, source) VALUES (?, ?, ?, ?, ?, ?)",
            ("dc-combo", "Win the game", "UB", "{U}",
             '["Have 0 cards in library"]', "spellbook")
        )
        self.conn.execute(
            "INSERT INTO combo_membership (oracle_id, combo_id, quantity) "
            "VALUES (?, ?, ?)",
            (THASSA_OID, "dc-combo", 1)
        )

        # Seed vanilla tag for Grizzly Bears (synthetic oracle_id)
        synthetic_bears = "grizzly-bears-synthetic"
        _seed_universe(self.conn, [(synthetic_bears, "Grizzly Bears")])
        self.conn.execute(
            "INSERT INTO tags (oracle_id, tag_name, source) "
            "VALUES (?, 'vanilla', 'scryfall_search')",
            (synthetic_bears,)
        )

        self.conn.commit()
        self.synthetic_bears = synthetic_bears

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sol_ring_is_universal_staple(self):
        card = self.repo.get_card(SOL_RING_OID)
        self.assertIsNotNone(card)
        self.assertTrue(card.staples["universal"],
                        "Sol Ring must be a universal staple")

    def test_sol_ring_is_cedh_staple(self):
        card = self.repo.get_card(SOL_RING_OID)
        self.assertIsNotNone(card)
        self.assertTrue(card.staples["cedh"],
                        "Sol Ring must be a cEDH staple")

    def test_sol_ring_has_salt(self):
        card = self.repo.get_card(SOL_RING_OID)
        self.assertIsNotNone(card)
        self.assertIsNotNone(card.salt)
        self.assertGreater(card.salt, 0)

    def test_thassas_oracle_in_cedh_and_combos(self):
        card = self.repo.get_card(THASSA_OID)
        self.assertIsNotNone(card)
        self.assertTrue(card.staples["cedh"],
                        "Thassa's Oracle must be a cEDH staple")
        self.assertGreater(len(card.combos), 0,
                           "Thassa's Oracle must be in at least one combo")

    def test_grizzly_bears_is_vanilla(self):
        card = self.repo.get_card(self.synthetic_bears)
        self.assertIsNotNone(card)
        self.assertIn("vanilla", card.tags)
        # No staple data
        self.assertFalse(card.staples.get("universal", False))
        self.assertFalse(card.staples.get("cedh", False))

    def test_get_staples_by_tier(self):
        universal = self.repo.get_staples("universal")
        self.assertIn(SOL_RING_OID, universal)

        cedh = self.repo.get_staples("cedh")
        self.assertIn(SOL_RING_OID, cedh)
        self.assertIn(THASSA_OID, cedh)

    def test_get_combo_members(self):
        members = self.repo.get_combo_members("dc-combo")
        self.assertIn(THASSA_OID, members)

    def test_unknown_oracle_id_returns_none(self):
        card = self.repo.get_card("completely-unknown-oid-xyz")
        self.assertIsNone(card)


if __name__ == "__main__":
    unittest.main()
