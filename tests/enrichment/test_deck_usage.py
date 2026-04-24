# tests/enrichment/test_deck_usage.py
# ---------------------------------------------------------------------------
# Phase 3 item 3.18 — deck_usage overlay tests.
#
# Tests cover:
#   - Population from an imported deck (cache_deck -> deck_usage rows)
#   - Population from a wishlist (cache_wishlist -> deck_usage rows)
#   - Combined counts (same oracle_id in both deck and wishlist)
#   - Idempotency (two refreshes produce identical DB state)
#   - Rebuild from scratch (refresh_deck_usage via EnrichmentRepo)
#   - is_used_in_deck() returns correct results
#   - is_cull_candidate() now correctly returns False when card is in deck
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import enrichment_db
from web_enrichment.repo import EnrichmentRepo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw_deck(oracle_ids: list[str]) -> str:
    """Build a minimal Moxfield deck JSON blob with oracle_ids in mainboard."""
    mainboard = {}
    for i, oid in enumerate(oracle_ids):
        mainboard[f"slot_{i}"] = {
            "quantity": 1,
            "card": {
                "oracle_id": oid,
                "name": f"Card_{oid[:8]}",
                "set": "tst",
                "cn": str(i + 1),
            },
        }
    return json.dumps({
        "id": "test-deck-id",
        "name": "Test Deck",
        "format": "commander",
        "createdByUser": {"userName": "testuser"},
        "mainboard": mainboard,
        "commanders": {},
        "sideboard": {},
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeckUsagePopulation(unittest.TestCase):
    """Tests for rebuild_deck_usage and the deck_usage table."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="deck_usage_test_")
        self.db_path = os.path.join(self.tmpdir, "enrichment.db")
        # Create a fresh DB with all tables including deck_usage.
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()
        self.repo = EnrichmentRepo(db_path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------

    def _get_conn(self):
        return enrichment_db.get_connection(db_path=self.db_path)

    def _insert_deck(self, deck_id: str, oracle_ids: list[str]):
        """Insert a minimal deck row directly into moxfield_decks."""
        conn = self._get_conn()
        try:
            with conn:
                conn.execute(
                    """INSERT OR REPLACE INTO moxfield_decks
                           (deck_id, deck_name, owner, last_fetched_at, format, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (deck_id, "Test Deck", "testuser", 0,
                     "commander", _make_raw_deck(oracle_ids)),
                )
        finally:
            conn.close()

    def _insert_wishlist(self, username: str, oracle_ids: list[str]):
        """Insert wishlist rows directly into moxfield_wishlists."""
        conn = self._get_conn()
        try:
            with conn:
                for i, oid in enumerate(oracle_ids):
                    conn.execute(
                        """INSERT OR REPLACE INTO moxfield_wishlists
                               (username, oracle_id, name, quantity,
                                set_code, collector_number, scryfall_id,
                                last_fetched_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (username, oid, f"Card_{oid[:8]}", 1,
                         "tst", str(i + 1), "", 0),
                    )
        finally:
            conn.close()

    def _deck_usage_row(self, oracle_id: str) -> dict | None:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM deck_usage WHERE oracle_id = ?",
                (oracle_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # -- deck import -> deck_usage --------------------------------------------

    def test_population_from_deck(self):
        """After inserting a deck and rebuilding, deck_count > 0."""
        self._insert_deck("deck1", ["oid-a", "oid-b", "oid-c"])
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            count = self.repo.refresh_deck_usage()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertEqual(count, 3)
        for oid in ("oid-a", "oid-b", "oid-c"):
            row = self._deck_usage_row(oid)
            self.assertIsNotNone(row, f"Expected row for {oid}")
            self.assertEqual(row["deck_count"], 1)
            self.assertEqual(row["wishlist_count"], 0)

    def test_population_from_wishlist(self):
        """After inserting a wishlist and rebuilding, wishlist_count > 0."""
        self._insert_wishlist("alice", ["oid-x", "oid-y"])
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            count = self.repo.refresh_deck_usage()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertEqual(count, 2)
        for oid in ("oid-x", "oid-y"):
            row = self._deck_usage_row(oid)
            self.assertIsNotNone(row)
            self.assertEqual(row["deck_count"], 0)
            self.assertEqual(row["wishlist_count"], 1)

    def test_combined_counts(self):
        """Card in both a deck and a wishlist gets deck_count=1, wishlist_count=1."""
        self._insert_deck("deck1", ["oid-shared", "oid-deck-only"])
        self._insert_wishlist("alice", ["oid-shared", "oid-wish-only"])

        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            count = self.repo.refresh_deck_usage()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertEqual(count, 3)

        shared = self._deck_usage_row("oid-shared")
        self.assertIsNotNone(shared)
        self.assertEqual(shared["deck_count"], 1)
        self.assertEqual(shared["wishlist_count"], 1)

        deck_only = self._deck_usage_row("oid-deck-only")
        self.assertIsNotNone(deck_only)
        self.assertEqual(deck_only["deck_count"], 1)
        self.assertEqual(deck_only["wishlist_count"], 0)

        wish_only = self._deck_usage_row("oid-wish-only")
        self.assertIsNotNone(wish_only)
        self.assertEqual(wish_only["deck_count"], 0)
        self.assertEqual(wish_only["wishlist_count"], 1)

    def test_deck_count_multiple_decks(self):
        """A card in two different decks gets deck_count=2."""
        self._insert_deck("deck1", ["oid-popular", "oid-a"])
        self._insert_deck("deck2", ["oid-popular", "oid-b"])

        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            self.repo.refresh_deck_usage()
        finally:
            enrichment_db.DB_PATH = orig

        popular = self._deck_usage_row("oid-popular")
        self.assertIsNotNone(popular)
        self.assertEqual(popular["deck_count"], 2)

    def test_wishlist_count_multiple_users(self):
        """A card on two users' wishlists gets wishlist_count=2."""
        self._insert_wishlist("alice", ["oid-wanted"])
        self._insert_wishlist("bob", ["oid-wanted"])

        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            self.repo.refresh_deck_usage()
        finally:
            enrichment_db.DB_PATH = orig

        wanted = self._deck_usage_row("oid-wanted")
        self.assertIsNotNone(wanted)
        self.assertEqual(wanted["wishlist_count"], 2)

    # -- idempotency ----------------------------------------------------------

    def test_idempotent_rebuild(self):
        """Two consecutive refresh_deck_usage() calls produce identical DB state."""
        self._insert_deck("deck1", ["oid-a", "oid-b"])
        self._insert_wishlist("alice", ["oid-b", "oid-c"])

        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            count1 = self.repo.refresh_deck_usage()
            rows1 = {
                row["oracle_id"]: (row["deck_count"], row["wishlist_count"])
                for row in self._all_deck_usage_rows()
            }

            count2 = self.repo.refresh_deck_usage()
            rows2 = {
                row["oracle_id"]: (row["deck_count"], row["wishlist_count"])
                for row in self._all_deck_usage_rows()
            }
        finally:
            enrichment_db.DB_PATH = orig

        self.assertEqual(count1, count2)
        self.assertEqual(rows1, rows2)

    def _all_deck_usage_rows(self) -> list[dict]:
        conn = self._get_conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM deck_usage ORDER BY oracle_id"
            ).fetchall()]
        finally:
            conn.close()

    # -- rebuild from scratch -------------------------------------------------

    def test_rebuild_from_scratch_clears_stale_rows(self):
        """Rebuilding after removing a deck removes the old oracle_id row."""
        self._insert_deck("deck1", ["oid-stale"])

        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            self.repo.refresh_deck_usage()
            self.assertIsNotNone(self._deck_usage_row("oid-stale"))

            # Delete the deck and rebuild.
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM moxfield_decks WHERE deck_id = 'deck1'")
                conn.commit()
            finally:
                conn.close()

            self.repo.refresh_deck_usage()
        finally:
            enrichment_db.DB_PATH = orig

        self.assertIsNone(self._deck_usage_row("oid-stale"))

    def test_empty_tables_returns_zero(self):
        """refresh_deck_usage() on empty tables writes zero rows."""
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            count = self.repo.refresh_deck_usage()
        finally:
            enrichment_db.DB_PATH = orig
        self.assertEqual(count, 0)

    # -- is_used_in_deck() ----------------------------------------------------

    def test_is_used_in_deck_true_for_deck(self):
        self._insert_deck("deck1", ["oid-deckcard"])
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            self.repo.refresh_deck_usage()
            result = self.repo.is_used_in_deck("oid-deckcard")
        finally:
            enrichment_db.DB_PATH = orig
        self.assertTrue(result)

    def test_is_used_in_deck_true_for_wishlist(self):
        self._insert_wishlist("alice", ["oid-wishcard"])
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            self.repo.refresh_deck_usage()
            result = self.repo.is_used_in_deck("oid-wishcard")
        finally:
            enrichment_db.DB_PATH = orig
        self.assertTrue(result)

    def test_is_used_in_deck_false_for_unknown(self):
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            result = self.repo.is_used_in_deck("oid-unknown")
        finally:
            enrichment_db.DB_PATH = orig
        self.assertFalse(result)

    # -- is_cull_candidate() with deck_usage ----------------------------------

    def test_cull_candidate_false_when_in_deck(self):
        """A vanilla card in a cached deck is NOT a cull candidate."""
        self._insert_deck("deck1", ["oid-vanilla-in-deck"])
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            self.repo.refresh_deck_usage()
            # oracle_text="" = vanilla
            result = self.repo.is_cull_candidate("oid-vanilla-in-deck",
                                                   oracle_text="")
        finally:
            enrichment_db.DB_PATH = orig
        self.assertFalse(result)

    def test_cull_candidate_false_when_in_wishlist(self):
        """A vanilla card on a wishlist is NOT a cull candidate."""
        self._insert_wishlist("alice", ["oid-vanilla-in-wish"])
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            self.repo.refresh_deck_usage()
            result = self.repo.is_cull_candidate("oid-vanilla-in-wish",
                                                   oracle_text="")
        finally:
            enrichment_db.DB_PATH = orig
        self.assertFalse(result)

    def test_cull_candidate_true_when_not_in_any_deck(self):
        """A vanilla card not in any deck IS a cull candidate (other criteria met)."""
        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            result = self.repo.is_cull_candidate("oid-orphan-vanilla",
                                                   oracle_text="")
        finally:
            enrichment_db.DB_PATH = orig
        self.assertTrue(result)


class TestDeckUsageAutoRefresh(unittest.TestCase):
    """Tests that cache_deck and cache_wishlist auto-rebuild deck_usage."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="deck_usage_auto_test_")
        self.db_path = os.path.join(self.tmpdir, "enrichment.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _deck_usage_count(self, oracle_id: str) -> tuple[int, int]:
        conn = enrichment_db.get_connection(db_path=self.db_path)
        try:
            row = conn.execute(
                "SELECT deck_count, wishlist_count FROM deck_usage "
                "WHERE oracle_id = ?",
                (oracle_id,),
            ).fetchone()
            return (row["deck_count"], row["wishlist_count"]) if row else (0, 0)
        finally:
            conn.close()

    def test_cache_deck_triggers_rebuild(self):
        """cache_deck() should rebuild deck_usage so is_used_in_deck works."""
        from web_enrichment.moxfield import cache_deck

        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            raw_data = json.loads(_make_raw_deck(["oid-auto"]))
            cache_deck("deck-auto", raw_data)
            dc, wc = self._deck_usage_count("oid-auto")
        finally:
            enrichment_db.DB_PATH = orig

        self.assertEqual(dc, 1)
        self.assertEqual(wc, 0)

    def test_cache_wishlist_triggers_rebuild(self):
        """cache_wishlist() should rebuild deck_usage so is_used_in_deck works."""
        from web_enrichment.moxfield import cache_wishlist

        cards = [{"oracle_id": "oid-wish-auto", "name": "Test", "quantity": 1,
                  "set": "tst", "collector_number": "1", "scryfall_id": ""}]

        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            cache_wishlist("testuser", cards)
            dc, wc = self._deck_usage_count("oid-wish-auto")
        finally:
            enrichment_db.DB_PATH = orig

        self.assertEqual(dc, 0)
        self.assertEqual(wc, 1)

    def test_re_import_deck_is_idempotent(self):
        """Importing the same deck twice leaves deck_count=1 (not 2)."""
        from web_enrichment.moxfield import cache_deck

        raw_data = json.loads(_make_raw_deck(["oid-idempotent"]))

        orig = enrichment_db.DB_PATH
        enrichment_db.DB_PATH = self.db_path
        try:
            cache_deck("deck-idempotent", raw_data)
            cache_deck("deck-idempotent", raw_data)
            dc, wc = self._deck_usage_count("oid-idempotent")
        finally:
            enrichment_db.DB_PATH = orig

        self.assertEqual(dc, 1)
        self.assertEqual(wc, 0)


if __name__ == "__main__":
    unittest.main()
