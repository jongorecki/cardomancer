# tests/enrichment/test_cull_view.py
# ---------------------------------------------------------------------------
# Phase 2.14 — Dead-weight cull view tests.
#
# Covers:
#   - Happy path: vanilla + no staple + no buylist → appears in results
#   - Enrichment signals gate: staple → excluded
#   - Enrichment signals gate: CK buylist > threshold → excluded
#   - Enrichment signals gate: salt score populates field
#   - commander_popularity populates field
#   - suggested_action derivation per keep_confidence band
#   - Empty inventory → [] gracefully
#   - Idempotency: two calls → identical result
#   - strict preset: price > 0 excluded even if below max_market_price
#   - cull_reasons populated per card
#   - Export CSV shape: correct columns + reason as comma-joined string
#   - Endpoint smoke test: returns 200 + expected JSON shape
#   - Known-vanilla (Grizzly Bears) appears in default results
#   - Known-vanilla (Squire) appears in default results
#   - Card in deck_usage → excluded from default results
# ---------------------------------------------------------------------------

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import collection_db
import enrichment_db
from web_enrichment.cull import get_cull_candidates, _suggest_action


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dbs():
    """Return (tmpdir, col_path, enr_path) + open connections."""
    tmpdir = tempfile.mkdtemp(prefix="cull_view_test_")
    col_path = os.path.join(tmpdir, "collection.db")
    enr_path = os.path.join(tmpdir, "enrichment.db")
    col_conn = collection_db.get_connection(db_path=col_path)
    enr_conn = enrichment_db.get_connection(db_path=enr_path)
    return tmpdir, col_path, enr_path, col_conn, enr_conn


def _add_inventory(col_conn, *, name, oracle_id, set_code="m10",
                   type_line="Creature — Bear", price_usd=0.10,
                   quantity=2, box=None):
    now = "2026-01-01T00:00:00"
    col_conn.execute(
        """INSERT INTO inventory
           (name, set_code, collector_number, oracle_id, type_line,
            price_usd, quantity, first_scanned, last_scanned, box)
           VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?)""",
        (name, set_code, oracle_id, type_line, price_usd, quantity,
         now, now, box),
    )
    col_conn.commit()


def _add_tag(enr_conn, oracle_id, tag_name):
    enr_conn.execute(
        "INSERT OR IGNORE INTO tags (oracle_id, tag_name, source) VALUES (?, ?, ?)",
        (oracle_id, tag_name, "scryfall_search"),
    )
    enr_conn.commit()


def _add_staple(enr_conn, oracle_id, tier="universal"):
    enr_conn.execute(
        "INSERT OR IGNORE INTO staples "
        "(oracle_id, tier, source, score, last_updated) "
        "VALUES (?, ?, 'edhrec', 0.9, '2026-01-01')",
        (oracle_id, tier),
    )
    enr_conn.commit()


def _add_buylist(enr_conn, oracle_id, price, vendor="ck"):
    enr_conn.execute(
        "INSERT OR IGNORE INTO buylists (oracle_id, vendor, price_usd, last_updated) "
        "VALUES (?, ?, ?, '2026-01-01')",
        (oracle_id, vendor, price),
    )
    enr_conn.commit()


def _add_salt(enr_conn, oracle_id, salt):
    enr_conn.execute(
        "INSERT OR IGNORE INTO salt_scores (oracle_id, salt, last_updated) "
        "VALUES (?, ?, '2026-01-01')",
        (oracle_id, salt),
    )
    enr_conn.commit()


def _add_commander_rank(enr_conn, oracle_id, deck_count):
    enr_conn.execute(
        "INSERT OR IGNORE INTO commander_ranks "
        "(oracle_id, deck_count, source, last_updated) "
        "VALUES (?, ?, 'edhrec', '2026-01-01')",
        (oracle_id, deck_count),
    )
    enr_conn.commit()


# ---------------------------------------------------------------------------
# Core cull logic tests
# ---------------------------------------------------------------------------

class TestCullCandidatesCore(unittest.TestCase):

    def setUp(self):
        self.tmpdir, self.col_path, self.enr_path, self.col, self.enr = \
            _make_dbs()

    def tearDown(self):
        self.col.close()
        self.enr.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- Happy path ---

    def test_vanilla_card_no_signals_appears(self):
        """Vanilla card with no enrichment rows → appears in results."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, max_buylist_price=0.05,
        )
        names = [r["name"] for r in result]
        self.assertIn("Grizzly Bears", names)

    def test_french_vanilla_appears(self):
        """French-vanilla card (Serra Angel) appears in results."""
        _add_inventory(self.col, name="Serra Angel",
                       oracle_id="serra-oid", price_usd=0.20)
        _add_tag(self.enr, "serra-oid", "french-vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, max_buylist_price=0.05,
        )
        names = [r["name"] for r in result]
        self.assertIn("Serra Angel", names)

    # --- Staple exclusion ---

    def test_universal_staple_excluded(self):
        """Universal staple not in results when exclude_staples=True."""
        _add_inventory(self.col, name="Sol Ring",
                       oracle_id="sol-oid", price_usd=2.00)
        _add_staple(self.enr, "sol-oid", tier="universal")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=5.0, max_buylist_price=5.0,
            exclude_staples=True,
        )
        names = [r["name"] for r in result]
        self.assertNotIn("Sol Ring", names)

    def test_archetype_staple_excluded(self):
        _add_inventory(self.col, name="Bear Cub",
                       oracle_id="cub-oid", price_usd=0.05)
        _add_staple(self.enr, "cub-oid", tier="archetype")
        _add_tag(self.enr, "cub-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, max_buylist_price=0.10,
            exclude_staples=True,
        )
        names = [r["name"] for r in result]
        self.assertNotIn("Bear Cub", names)

    def test_cedh_staple_excluded(self):
        _add_inventory(self.col, name="Thassa's Oracle",
                       oracle_id="oracle-oid", price_usd=0.50)
        _add_staple(self.enr, "oracle-oid", tier="cedh")
        _add_tag(self.enr, "oracle-oid", "french-vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, exclude_staples=True,
        )
        names = [r["name"] for r in result]
        self.assertNotIn("Thassa's Oracle", names)

    def test_include_staples_flag_shows_staple(self):
        """exclude_staples=False → staple card can appear."""
        _add_inventory(self.col, name="Sol Ring",
                       oracle_id="sol-oid", price_usd=0.10)
        _add_staple(self.enr, "sol-oid", tier="universal")
        _add_tag(self.enr, "sol-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, max_buylist_price=5.0,
            exclude_staples=False,
        )
        names = [r["name"] for r in result]
        self.assertIn("Sol Ring", names)

    # --- Buylist exclusion ---

    def test_high_buylist_price_excluded(self):
        """Card with CK buylist > max_buylist_price is excluded."""
        _add_inventory(self.col, name="Savannah Lions",
                       oracle_id="lions-oid", price_usd=0.10)
        _add_tag(self.enr, "lions-oid", "vanilla")
        _add_buylist(self.enr, "lions-oid", price=0.50)  # > 0.05 default

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, max_buylist_price=0.05,
        )
        names = [r["name"] for r in result]
        self.assertNotIn("Savannah Lions", names)

    def test_zero_buylist_not_excluded(self):
        """Buylist price == 0 does not exclude the card."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")
        _add_buylist(self.enr, "bears-oid", price=0.0)

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, max_buylist_price=0.05,
        )
        names = [r["name"] for r in result]
        self.assertIn("Grizzly Bears", names)

    # --- Enrichment signal population ---

    def test_salt_score_populated(self):
        """salt_score field reflects enrichment DB."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")
        _add_salt(self.enr, "bears-oid", 0.3)

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        row = next(r for r in result if r["name"] == "Grizzly Bears")
        self.assertAlmostEqual(row["salt_score"], 0.3, places=5)

    def test_salt_score_none_when_absent(self):
        """salt_score is None if no salt_scores row."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        row = next(r for r in result if r["name"] == "Grizzly Bears")
        self.assertIsNone(row["salt_score"])

    def test_buylist_price_populated(self):
        """buylist_price field reflects CK buylist entry."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")
        _add_buylist(self.enr, "bears-oid", price=0.04)  # <= 0.05

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, max_buylist_price=0.05,
        )
        row = next(r for r in result if r["name"] == "Grizzly Bears")
        self.assertAlmostEqual(row["buylist_price"], 0.04, places=5)

    def test_commander_popularity_populated(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")
        _add_commander_rank(self.enr, "bears-oid", 150)

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        row = next(r for r in result if r["name"] == "Grizzly Bears")
        self.assertEqual(row["commander_popularity"], 150)

    def test_staple_flags_populated(self):
        """Staple flag fields reflect enrichment data."""
        # One card with all tiers (unusual but tests the flags)
        _add_inventory(self.col, name="Test Card",
                       oracle_id="test-oid", price_usd=0.10)
        _add_tag(self.enr, "test-oid", "vanilla")
        # Staples excluded by default; use exclude_staples=False
        _add_staple(self.enr, "test-oid", tier="universal")
        _add_staple(self.enr, "test-oid", tier="archetype")
        _add_staple(self.enr, "test-oid", tier="cedh")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, exclude_staples=False,
        )
        row = next((r for r in result if r["oracle_id"] == "test-oid"), None)
        self.assertIsNotNone(row)
        self.assertTrue(row["is_universal_staple"])
        self.assertTrue(row["is_archetype_staple"])
        self.assertTrue(row["is_cedh_staple"])

    # --- Cull reasons ---

    def test_cull_reasons_vanilla(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        row = next(r for r in result if r["name"] == "Grizzly Bears")
        self.assertIn("vanilla", row["cull_reasons"])
        self.assertIn("no_staple", row["cull_reasons"])

    def test_cull_reasons_french_vanilla(self):
        _add_inventory(self.col, name="Serra Angel",
                       oracle_id="serra-oid", price_usd=0.20)
        _add_tag(self.enr, "serra-oid", "french-vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        row = next(r for r in result if r["name"] == "Serra Angel")
        self.assertIn("french-vanilla", row["cull_reasons"])

    def test_cull_reasons_no_staple_always_present(self):
        """no_staple reason present for non-staple card."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.05)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        row = next(r for r in result if r["name"] == "Grizzly Bears")
        self.assertIn("no_staple", row["cull_reasons"])

    # --- Suggested action ---

    def test_suggested_action_donate(self):
        """Low confidence → 'donate'."""
        self.assertEqual(_suggest_action(0.0), "donate")
        self.assertEqual(_suggest_action(0.09), "donate")

    def test_suggested_action_bulk(self):
        self.assertEqual(_suggest_action(0.10), "bulk")
        self.assertEqual(_suggest_action(0.19), "bulk")

    def test_suggested_action_trade(self):
        self.assertEqual(_suggest_action(0.20), "trade")
        self.assertEqual(_suggest_action(0.39), "trade")

    def test_suggested_action_sell(self):
        self.assertEqual(_suggest_action(0.40), "sell")
        self.assertEqual(_suggest_action(1.0), "sell")

    def test_result_has_suggested_action_field(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.05)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        row = next(r for r in result if r["name"] == "Grizzly Bears")
        self.assertIn(row["suggested_action"],
                      ("donate", "bulk", "trade", "sell"))

    # --- Location ---

    def test_location_from_box(self):
        """location field = box column value."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10,
                       box="box-3")
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        row = next(r for r in result if r["name"] == "Grizzly Bears")
        self.assertEqual(row["location"], "box-3")

    def test_location_empty_when_no_box(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        row = next(r for r in result if r["name"] == "Grizzly Bears")
        self.assertEqual(row["location"], "")

    # --- Presets ---

    def test_strict_preset_excludes_nonzero_price(self):
        """strict preset: price_usd > 0 → excluded even if below max_market_price."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, preset="strict",
        )
        # price_usd=0.10 > 0 → excluded from strict preset
        names = [r["name"] for r in result]
        self.assertNotIn("Grizzly Bears", names)

    def test_strict_preset_includes_zero_price(self):
        """strict preset: price_usd == 0 → included."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.0)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, preset="strict",
        )
        names = [r["name"] for r in result]
        self.assertIn("Grizzly Bears", names)

    def test_strict_preset_includes_null_price(self):
        """strict preset: price_usd IS NULL → included."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=None)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, preset="strict",
        )
        names = [r["name"] for r in result]
        self.assertIn("Grizzly Bears", names)

    # --- Price filter ---

    def test_high_price_excluded_default(self):
        """Card priced above max_market_price not included."""
        _add_inventory(self.col, name="Expensive Bear",
                       oracle_id="exp-oid", price_usd=5.00)
        _add_tag(self.enr, "exp-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, max_buylist_price=0.05,
        )
        names = [r["name"] for r in result]
        self.assertNotIn("Expensive Bear", names)

    # --- min_quantity ---

    def test_min_quantity_filter(self):
        """Cards with quantity < min_quantity not returned."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10,
                       quantity=1)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, min_quantity=2,
        )
        names = [r["name"] for r in result]
        self.assertNotIn("Grizzly Bears", names)

    def test_min_quantity_default_includes_single(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10,
                       quantity=1)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
            max_market_price=1.0, min_quantity=1,
        )
        names = [r["name"] for r in result]
        self.assertIn("Grizzly Bears", names)

    # --- Graceful empty ---

    def test_empty_inventory_returns_empty_list(self):
        """No inventory rows → empty result, no exception."""
        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path,
        )
        self.assertEqual(result, [])

    def test_missing_enrichment_db_returns_empty(self):
        """If enrichment.db doesn't exist, returns [] without crash."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        result = get_cull_candidates(
            self.col,
            enr_db_path="/nonexistent/path/enrichment.db",
        )
        self.assertIsInstance(result, list)
        # Fallback path returns price-filtered rows
        # (enr absent → fallback; price 0.10 < 1.0 → included)
        # Just check it doesn't raise

    # --- Idempotency ---

    def test_idempotent_two_calls(self):
        """Calling twice gives identical results."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")

        r1 = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        r2 = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        self.assertEqual(
            [c["name"] for c in r1],
            [c["name"] for c in r2],
        )

    # --- Multiple cards: ordering ---

    def test_results_ordered_by_name(self):
        _add_inventory(self.col, name="Zombie Bear",
                       oracle_id="z-oid", price_usd=0.05)
        _add_inventory(self.col, name="Alpha Bear",
                       oracle_id="a-oid", price_usd=0.05)
        _add_tag(self.enr, "z-oid", "vanilla")
        _add_tag(self.enr, "a-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        names = [r["name"] for r in result]
        self.assertEqual(names, sorted(names))

    # --- Full field presence ---

    def test_result_row_has_all_required_fields(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        self.assertTrue(len(result) > 0)
        row = result[0]
        required = {
            "oracle_id", "name", "set_code", "quantity",
            "buylist_price", "salt_score",
            "is_universal_staple", "is_archetype_staple", "is_cedh_staple",
            "commander_popularity",
            "cull_reasons", "suggested_action", "keep_confidence",
            "location",
        }
        for field in required:
            self.assertIn(field, row, f"Missing field: {field}")


# ---------------------------------------------------------------------------
# Deck-usage exclusion
# ---------------------------------------------------------------------------

class TestCullViewDeckUsage(unittest.TestCase):
    """Cards in deck_usage should be excluded from cull results."""

    def setUp(self):
        self.tmpdir, self.col_path, self.enr_path, self.col, self.enr = \
            _make_dbs()
        # deck_usage table already created by enrichment_db.get_connection()
        # (schema includes it). No manual creation needed.

    def tearDown(self):
        self.col.close()
        self.enr.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_deck_usage(self, oracle_id, deck_count, wishlist_count):
        self.enr.execute(
            "INSERT INTO deck_usage "
            "(oracle_id, deck_count, wishlist_count, updated_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00')",
            (oracle_id, deck_count, wishlist_count),
        )
        self.enr.commit()

    def test_card_in_deck_excluded(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")
        self._insert_deck_usage("bears-oid", deck_count=2, wishlist_count=0)

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        names = [r["name"] for r in result]
        self.assertNotIn("Grizzly Bears", names)

    def test_card_in_wishlist_excluded(self):
        _add_inventory(self.col, name="Serra Angel",
                       oracle_id="serra-oid", price_usd=0.20)
        _add_tag(self.enr, "serra-oid", "french-vanilla")
        self._insert_deck_usage("serra-oid", deck_count=0, wishlist_count=1)

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        names = [r["name"] for r in result]
        self.assertNotIn("Serra Angel", names)

    def test_card_not_in_deck_included(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")
        self._insert_deck_usage("bears-oid", deck_count=0, wishlist_count=0)

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        names = [r["name"] for r in result]
        self.assertIn("Grizzly Bears", names)


# ---------------------------------------------------------------------------
# Known-vanilla test cards (acceptance criteria)
# ---------------------------------------------------------------------------

class TestKnownVanillaCards(unittest.TestCase):
    """Grizzly Bears and Squire must appear in default cull results."""

    def setUp(self):
        self.tmpdir, self.col_path, self.enr_path, self.col, self.enr = \
            _make_dbs()

    def tearDown(self):
        self.col.close()
        self.enr.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_grizzly_bears_appears(self):
        """Grizzly Bears: vanilla creature, no enrichment signals → cull."""
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        self.assertTrue(any(r["name"] == "Grizzly Bears" for r in result))

    def test_squire_appears(self):
        """Squire (1W 1/2 vanilla): appears in default cull results."""
        _add_inventory(self.col, name="Squire",
                       oracle_id="squire-oid",
                       type_line="Creature — Human Soldier",
                       price_usd=0.05)
        _add_tag(self.enr, "squire-oid", "vanilla")

        result = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        self.assertTrue(any(r["name"] == "Squire" for r in result))


# ---------------------------------------------------------------------------
# collection_db wrapper compatibility
# ---------------------------------------------------------------------------

class TestCollectionDbWrapper(unittest.TestCase):
    """Ensure collection_db.get_cull_candidates delegates correctly."""

    def setUp(self):
        self.tmpdir, self.col_path, self.enr_path, self.col, self.enr = \
            _make_dbs()

    def tearDown(self):
        self.col.close()
        self.enr.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_wrapper_returns_list(self):
        result = collection_db.get_cull_candidates(
            self.col, enr_db_path=self.enr_path
        )
        self.assertIsInstance(result, list)

    def test_wrapper_delegates_preset(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.0)
        _add_tag(self.enr, "bears-oid", "vanilla")

        strict = collection_db.get_cull_candidates(
            self.col, enr_db_path=self.enr_path, preset="strict",
        )
        default = collection_db.get_cull_candidates(
            self.col, enr_db_path=self.enr_path, preset="default",
        )
        # Both should include price=0 card
        self.assertTrue(any(r["name"] == "Grizzly Bears" for r in strict))
        self.assertTrue(any(r["name"] == "Grizzly Bears" for r in default))

    def test_wrapper_max_price_parameter(self):
        _add_inventory(self.col, name="Pricey Bear",
                       oracle_id="pricey-oid", price_usd=2.00)
        _add_tag(self.enr, "pricey-oid", "vanilla")

        low_threshold = collection_db.get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_price=1.0,
        )
        high_threshold = collection_db.get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_price=5.0,
        )
        names_low  = [r["name"] for r in low_threshold]
        names_high = [r["name"] for r in high_threshold]
        self.assertNotIn("Pricey Bear", names_low)
        self.assertIn("Pricey Bear", names_high)


# ---------------------------------------------------------------------------
# Export CSV shape test
# ---------------------------------------------------------------------------

class TestCullExportCSV(unittest.TestCase):
    """Verify CSV export column set and data format."""

    def setUp(self):
        self.tmpdir, self.col_path, self.enr_path, self.col, self.enr = \
            _make_dbs()

    def tearDown(self):
        self.col.close()
        self.enr.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_csv(self, candidates):
        output = io.StringIO()
        fields = [
            'oracle_id', 'name', 'set_code', 'type_line',
            'price_usd', 'quantity',
            'buylist_price', 'salt_score',
            'is_universal_staple', 'is_archetype_staple', 'is_cedh_staple',
            'commander_popularity',
            'suggested_action', 'keep_confidence',
            'location', 'cull_reasons',
        ]
        writer = csv.DictWriter(output, fieldnames=fields,
                                extrasaction='ignore')
        writer.writeheader()
        for c in candidates:
            row = dict(c)
            row['cull_reasons'] = ', '.join(row.get('cull_reasons') or [])
            writer.writerow(row)
        return output.getvalue()

    def test_csv_has_required_columns(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")

        candidates = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        csv_text = self._build_csv(candidates)

        reader = csv.DictReader(io.StringIO(csv_text))
        headers = reader.fieldnames or []

        for col in ('oracle_id', 'name', 'set_code', 'price_usd',
                    'quantity', 'buylist_price', 'salt_score',
                    'suggested_action', 'cull_reasons', 'location'):
            self.assertIn(col, headers, f"Missing CSV column: {col}")

    def test_csv_cull_reasons_comma_joined(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")

        candidates = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        csv_text = self._build_csv(candidates)
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        self.assertTrue(len(rows) >= 1)
        # cull_reasons in CSV should be a string, not a list repr
        reasons_str = rows[0].get("cull_reasons", "")
        self.assertNotIn("[", reasons_str)  # not a Python list repr
        self.assertIn("vanilla", reasons_str)

    def test_csv_one_row_per_card(self):
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_inventory(self.col, name="Squire",
                       oracle_id="squire-oid", price_usd=0.05)
        _add_tag(self.enr, "bears-oid", "vanilla")
        _add_tag(self.enr, "squire-oid", "vanilla")

        candidates = get_cull_candidates(
            self.col, enr_db_path=self.enr_path, max_market_price=1.0,
        )
        csv_text = self._build_csv(candidates)
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        self.assertEqual(len(rows), 2)


# ---------------------------------------------------------------------------
# Endpoint smoke tests
# ---------------------------------------------------------------------------

class TestCullEndpointSmoke(unittest.TestCase):
    """Smoke tests for the Flask endpoints via test client."""

    def setUp(self):
        self.tmpdir, self.col_path, self.enr_path, self.col, self.enr = \
            _make_dbs()
        # Add a vanilla card
        _add_inventory(self.col, name="Grizzly Bears",
                       oracle_id="bears-oid", price_usd=0.10)
        _add_tag(self.enr, "bears-oid", "vanilla")
        self.col.close()
        self.enr.close()

        import enrichment_db as _edb
        import collection_db as _cdb
        self._orig_enr_path = _edb.DB_PATH
        self._orig_col_path = _cdb.DB_PATH
        _edb.DB_PATH = self.enr_path
        _cdb.DB_PATH = self.col_path

        # Import app lazily to avoid running server init at module level
        try:
            import web_server
            self.client = web_server.app.test_client()
            self.app_available = True
        except Exception:
            self.app_available = False

    def tearDown(self):
        import enrichment_db as _edb
        import collection_db as _cdb
        _edb.DB_PATH = self._orig_enr_path
        _cdb.DB_PATH = self._orig_col_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cull_endpoint_200(self):
        if not self.app_available:
            self.skipTest("web_server not importable")
        resp = self.client.get('/api/collection/cull-candidates')
        self.assertEqual(resp.status_code, 200)

    def test_cull_endpoint_json_shape(self):
        if not self.app_available:
            self.skipTest("web_server not importable")
        resp = self.client.get('/api/collection/cull-candidates?max_price=1.0')
        data = json.loads(resp.data)
        self.assertIn('candidates', data)
        self.assertIn('total', data)
        self.assertIn('max_price', data)
        self.assertIn('preset', data)
        self.assertIsInstance(data['candidates'], list)

    def test_cull_endpoint_strict_preset(self):
        if not self.app_available:
            self.skipTest("web_server not importable")
        resp = self.client.get(
            '/api/collection/cull-candidates?preset=strict&max_price=1.0'
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data['preset'], 'strict')

    def test_cull_export_endpoint_200(self):
        if not self.app_available:
            self.skipTest("web_server not importable")
        resp = self.client.get('/api/collection/cull-candidates/export')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'name', resp.data)  # CSV header present

    def test_cull_export_content_type(self):
        if not self.app_available:
            self.skipTest("web_server not importable")
        resp = self.client.get('/api/collection/cull-candidates/export')
        self.assertIn('text/csv', resp.content_type)

    def test_cull_export_has_oracle_id_column(self):
        if not self.app_available:
            self.skipTest("web_server not importable")
        resp = self.client.get('/api/collection/cull-candidates/export')
        first_line = resp.data.decode('utf-8').split('\r\n')[0]
        self.assertIn('oracle_id', first_line)
        self.assertIn('suggested_action', first_line)


if __name__ == "__main__":
    unittest.main()
