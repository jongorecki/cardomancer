# tests/enrichment/test_moxfield.py
# ---------------------------------------------------------------------------
# Unit tests for web_enrichment/moxfield.py.
#
# Uses the DB_PATH redirect pattern (NOT patching get_connection) because
# MoxfieldSource calls conn.close() in finally blocks; a patched connection
# object would be invalidated by that call.
#
# All HTTP calls are mocked — no live Moxfield traffic.
#
# Coverage:
#   - URL/ID extraction
#   - parse_deck_cards: basic, sideboard, printing_mode, basic-land exclusion
#   - parse_wishlist_cards: board-map format, items-array fallback
#   - cache_deck / get_cached_deck (idempotency, miss, hit)
#   - cache_wishlist / get_cached_wishlist (atomic replace, miss, hit)
#   - MoxfieldSource.import_deck (happy path, cache hit, idempotency,
#                                  printing_mode, basic-land exclusion)
#   - MoxfieldSource.import_wishlist (happy path, cache hit, printing_mode)
#   - printing_mode=exact matching on (set, cn)
#   - printing_mode=any fallback when set/cn missing
#   - Basic-land exclusion: Forest, Island, Mountain, Plains, Swamp, Wastes,
#     snow basic (Snowcovered Forest); dual-face where BACK face is basic land
#     but front face is not (must be INCLUDED — exclusion is on front oracle_id)
#   - is_in_deck / is_in_wishlist helpers
#   - Probe delegates
#   - Refresh no-op stub updates sync_metadata
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
    PRINTING_MODE_ANY,
    PRINTING_MODE_EXACT,
    _format_import_result,
    _format_wishlist_result,
    _build_oracle_type_index,
    cache_deck,
    cache_wishlist,
    extract_deck_id,
    fetch_deck,
    fetch_wishlist,
    get_cached_deck,
    get_cached_wishlist,
    is_basic_land,
    is_in_deck,
    is_in_wishlist,
    parse_deck_cards,
    parse_wishlist_cards,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moxfield"

# Oracle IDs for well-known basic lands (used in exclusion tests).
# These match the fixture files and are just test data — they are NOT real
# Scryfall oracle IDs; the test fakes the card-data index.
FOREST_OID      = "ce7b1f32-a1ec-4fe3-bb46-7a7c79c7ad61"
ISLAND_OID      = "b2c6aa39-2d21-4f2e-b5ca-c0ccb538f9d4"   # reused as Wastes in fixture
WASTES_OID      = "b2c6aa39-2d21-4f2e-b5ca-c0ccb538f9d4"
SNOW_FOREST_OID = "ffffffff-ffff-ffff-ffff-000000000001"    # synthetic
SOL_RING_OID    = "a2e0e217-6a60-4c74-a350-2f099b8e6f00"
BOLT_OID        = "8b4d282e-06fb-4774-a657-5d4a9a41b52e"
ATRAXA_OID      = "21935e54-c2a8-4756-8547-1ece5b07d0fa"
MYSTERIOUS_OID  = "abcd1234-0000-0000-0000-000000000001"

# Synthetic oracle index mapping oracle_id → type_line for tests.
# Covers all cards in the fixtures plus extra basics for edge-case tests.
_FAKE_ORACLE_TYPE_INDEX = {
    FOREST_OID:      "Basic Land — Forest",
    WASTES_OID:      "Basic Land",                  # Wastes has no subtype
    SNOW_FOREST_OID: "Basic Snow Land — Forest",
    SOL_RING_OID:    "Artifact",
    BOLT_OID:        "Instant",
    ATRAXA_OID:      "Legendary Creature — Phyrexian Angel Horror",
    # Plains/Island/Mountain/Swamp also need entries for the exclusion tests
    "plains-oid-001":   "Basic Land — Plains",
    "island-oid-001":   "Basic Land — Island",
    "mountain-oid-001": "Basic Land — Mountain",
    "swamp-oid-001":    "Basic Land — Swamp",
    # Dual-face card whose BACK face is a basic land but whose FRONT oracle_id
    # is not a basic.  e.g., "Valakut Awakening // Valakut Stoneforge" —
    # the front is a Sorcery, should NOT be excluded.
    "dfc-front-oid":    "Sorcery",
}


def _patch_oracle_index(func):
    """Decorator: patch the module-level oracle type index with fake data."""
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        import web_enrichment.moxfield as _mod
        orig = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX
        try:
            return func(*args, **kwargs)
        finally:
            _mod._oracle_type_index = orig
    return wrapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_deck_fixture() -> dict:
    p = FIXTURES / "deck_sample.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _load_wishlist_fixture() -> dict:
    p = FIXTURES / "wishlist_sample.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _direct_query(db_path: str, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DB test base
# ---------------------------------------------------------------------------

class _DBTestCase(unittest.TestCase):
    """Base class: redirects enrichment_db.DB_PATH to a temp file."""

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
        self.assertIsNone(extract_deck_id("abc"))

    def test_deck_url_without_query_string(self):
        url = "https://www.moxfield.com/decks/lzbasAFQhEqY5x5SmJRZ9w"
        self.assertIsNotNone(extract_deck_id(url))


# ---------------------------------------------------------------------------
# Test: is_basic_land
# ---------------------------------------------------------------------------

class TestIsBasicLand(unittest.TestCase):
    """Verify the basic-land exclusion helper against the fake index."""

    def setUp(self):
        import web_enrichment.moxfield as _mod
        self._orig = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX

    def tearDown(self):
        import web_enrichment.moxfield as _mod
        _mod._oracle_type_index = self._orig

    def test_forest_is_basic(self):
        self.assertTrue(is_basic_land(FOREST_OID))

    def test_wastes_is_basic(self):
        self.assertTrue(is_basic_land(WASTES_OID, "Wastes"))

    def test_snow_forest_is_basic(self):
        self.assertTrue(is_basic_land(SNOW_FOREST_OID, "Snow-Covered Forest"))

    def test_plains_is_basic(self):
        self.assertTrue(is_basic_land("plains-oid-001", "Plains"))

    def test_island_is_basic(self):
        self.assertTrue(is_basic_land("island-oid-001", "Island"))

    def test_mountain_is_basic(self):
        self.assertTrue(is_basic_land("mountain-oid-001", "Mountain"))

    def test_swamp_is_basic(self):
        self.assertTrue(is_basic_land("swamp-oid-001", "Swamp"))

    def test_sol_ring_not_basic(self):
        self.assertFalse(is_basic_land(SOL_RING_OID, "Sol Ring"))

    def test_atraxa_not_basic(self):
        self.assertFalse(is_basic_land(ATRAXA_OID, "Atraxa, Praetors' Voice"))

    def test_dfc_front_is_sorcery_not_basic(self):
        """A DFC whose front face is a Sorcery must NOT be excluded,
        even if its back face happens to be a basic land.  Exclusion
        is keyed on the oracle_id which Moxfield provides for the front face."""
        self.assertFalse(is_basic_land("dfc-front-oid", "DFC Front"))

    def test_unknown_oracle_id_returns_false(self):
        """Cards not in the local cache must be included (not excluded)."""
        self.assertFalse(is_basic_land("00000000-unknown-card", "Unknown Card"))


# ---------------------------------------------------------------------------
# Test: parse_deck_cards
# ---------------------------------------------------------------------------

class TestParseDeckCards(unittest.TestCase):

    def setUp(self):
        self.data = _load_deck_fixture()
        # Patch the oracle type index so Forest gets excluded.
        import web_enrichment.moxfield as _mod
        self._orig = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX

    def tearDown(self):
        import web_enrichment.moxfield as _mod
        _mod._oracle_type_index = self._orig

    def test_returns_tuple_cards_warnings(self):
        result = parse_deck_cards(self.data)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_mainboard_and_commanders_merged(self):
        cards, _ = parse_deck_cards(self.data, exclude_basics=False)
        boards = {c["board"] for c in cards}
        self.assertIn("mainboard", boards)
        self.assertIn("commanders", boards)

    def test_basic_lands_excluded_by_default(self):
        cards, _ = parse_deck_cards(self.data)
        names = {c["name"] for c in cards}
        self.assertNotIn("Forest", names)

    def test_basic_lands_included_when_exclude_false(self):
        cards, _ = parse_deck_cards(self.data, exclude_basics=False)
        names = {c["name"] for c in cards}
        self.assertIn("Forest", names)

    def test_sideboard_excluded_by_default(self):
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
        cards, _ = parse_deck_cards(self.data, include_side=False)
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
        cards, _ = parse_deck_cards(self.data, include_side=True)
        boards = {c["board"] for c in cards}
        self.assertIn("sideboard", boards)

    def test_quantity_preserved(self):
        # Forest is excluded by default, so check with exclude_basics=False
        cards, _ = parse_deck_cards(self.data, exclude_basics=False)
        forest = next(c for c in cards if c["name"] == "Forest")
        self.assertEqual(forest["quantity"], 3)

    def test_cards_missing_oracle_id_skipped(self):
        self.data["mainboard"]["BAD"] = {
            "quantity": 1,
            "card": {"name": "No Oracle Card"},
        }
        cards, _ = parse_deck_cards(self.data)
        names = {c["name"] for c in cards}
        self.assertNotIn("No Oracle Card", names)

    def test_oracle_id_populated(self):
        cards, _ = parse_deck_cards(self.data)
        for c in cards:
            self.assertNotEqual(c["oracle_id"], "")

    def test_collector_number_mapped_from_cn(self):
        cards, _ = parse_deck_cards(self.data)
        sol = next(c for c in cards if c["name"] == "Sol Ring")
        self.assertEqual(sol["collector_number"], "472")

    def test_commander_in_output(self):
        cards, _ = parse_deck_cards(self.data)
        commanders = [c for c in cards if c["board"] == "commanders"]
        self.assertEqual(len(commanders), 1)
        self.assertEqual(commanders[0]["name"], "Atraxa, Praetors' Voice")

    # --- printing_mode tests ---

    def test_printing_mode_any_card_has_mode_field(self):
        cards, _ = parse_deck_cards(self.data, printing_mode=PRINTING_MODE_ANY)
        for c in cards:
            self.assertEqual(c["printing_mode"], PRINTING_MODE_ANY)

    def test_printing_mode_exact_card_has_mode_field(self):
        cards, _ = parse_deck_cards(self.data, printing_mode=PRINTING_MODE_EXACT)
        # Cards with set+cn should have exact mode; missing set/cn should
        # have fallen back to any.
        for c in cards:
            if c["set"] and c["collector_number"]:
                self.assertEqual(c["printing_mode"], PRINTING_MODE_EXACT)

    def test_printing_mode_exact_fallback_when_set_missing(self):
        """Card without set/cn falls back to 'any' and generates a warning."""
        # Inject a card with missing set+cn
        self.data["mainboard"]["NOSETCN"] = {
            "quantity": 1,
            "card": {
                "oracle_id": "11111111-1111-1111-1111-111111111111",
                "name": "Mysterious Spell",
                "set": "",
                "cn": "",
                "scryfall_id": "abc",
            },
        }
        cards, warnings = parse_deck_cards(self.data,
                                           printing_mode=PRINTING_MODE_EXACT)
        mystery = next(
            (c for c in cards
             if c["oracle_id"] == "11111111-1111-1111-1111-111111111111"),
            None,
        )
        self.assertIsNotNone(mystery)
        self.assertEqual(mystery["printing_mode"], PRINTING_MODE_ANY)
        # Warning must mention the card name and fallback
        self.assertTrue(
            any("Mysterious Spell" in w or "oracle_id" in w.lower()
                for w in warnings)
        )

    def test_warnings_empty_for_any_mode(self):
        _, warnings = parse_deck_cards(self.data, printing_mode=PRINTING_MODE_ANY)
        self.assertEqual(warnings, [])


# ---------------------------------------------------------------------------
# Test: parse_wishlist_cards
# ---------------------------------------------------------------------------

class TestParseWishlistCards(unittest.TestCase):

    def setUp(self):
        self.data = _load_wishlist_fixture()
        import web_enrichment.moxfield as _mod
        self._orig = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX

    def tearDown(self):
        import web_enrichment.moxfield as _mod
        _mod._oracle_type_index = self._orig

    def test_returns_tuple(self):
        result = parse_wishlist_cards(self.data)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_basic_lands_excluded_by_default(self):
        cards, _ = parse_wishlist_cards(self.data)
        names = {c["name"] for c in cards}
        self.assertNotIn("Forest", names)
        self.assertNotIn("Wastes", names)

    def test_basic_lands_included_when_exclude_false(self):
        cards, _ = parse_wishlist_cards(self.data, exclude_basics=False)
        names = {c["name"] for c in cards}
        self.assertIn("Forest", names)
        self.assertIn("Wastes", names)

    def test_nonbasic_cards_included(self):
        cards, _ = parse_wishlist_cards(self.data)
        names = {c["name"] for c in cards}
        self.assertIn("Sol Ring", names)
        self.assertIn("Lightning Bolt", names)
        self.assertIn("Atraxa, Praetors' Voice", names)

    def test_quantity_preserved(self):
        cards, _ = parse_wishlist_cards(self.data, exclude_basics=False)
        bolt = next(c for c in cards if c["name"] == "Lightning Bolt")
        self.assertEqual(bolt["quantity"], 2)

    def test_printing_mode_exact_with_set_cn(self):
        cards, warnings = parse_wishlist_cards(self.data,
                                               printing_mode=PRINTING_MODE_EXACT)
        # Cards that have set+cn should be in exact mode.
        sol = next((c for c in cards if c["name"] == "Sol Ring"), None)
        self.assertIsNotNone(sol)
        self.assertEqual(sol["printing_mode"], PRINTING_MODE_EXACT)
        self.assertEqual(sol["set"], "clb")
        self.assertEqual(sol["collector_number"], "472")

    def test_printing_mode_exact_fallback_missing_set_cn(self):
        """Card with empty set/cn falls back to 'any' and emits a warning."""
        cards, warnings = parse_wishlist_cards(self.data,
                                               printing_mode=PRINTING_MODE_EXACT)
        mystery = next(
            (c for c in cards if c["name"] == "Mysterious Card"), None
        )
        self.assertIsNotNone(mystery)
        self.assertEqual(mystery["printing_mode"], PRINTING_MODE_ANY)
        self.assertTrue(len(warnings) > 0)
        self.assertTrue(any("Mysterious Card" in w for w in warnings))

    def test_items_array_fallback(self):
        """parse_wishlist_cards handles 'items' list format too."""
        alt_data = {
            "items": [
                {
                    "quantity": 3,
                    "card": {
                        "oracle_id": SOL_RING_OID,
                        "name": "Sol Ring",
                        "set": "clb",
                        "cn": "472",
                        "scryfall_id": "7e78b70b",
                    },
                }
            ]
        }
        cards, _ = parse_wishlist_cards(alt_data)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["name"], "Sol Ring")
        self.assertEqual(cards[0]["quantity"], 3)

    def test_empty_response_returns_empty_list(self):
        cards, warnings = parse_wishlist_cards({})
        self.assertEqual(cards, [])
        self.assertEqual(warnings, [])


# ---------------------------------------------------------------------------
# Test: cache_deck and get_cached_deck
# ---------------------------------------------------------------------------

class TestDeckCache(_DBTestCase):

    def _load_fixture(self):
        return _load_deck_fixture()

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
        cache_deck(data["id"], data)
        rows = _direct_query(self.db_path,
                             "SELECT * FROM moxfield_decks WHERE deck_id = ?",
                             (data["id"],))
        self.assertEqual(len(rows), 1)

    def test_get_cached_deck_hit(self):
        data = self._load_fixture()
        cache_deck(data["id"], data)
        import web_enrichment.moxfield as _mod
        orig = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX
        try:
            result = get_cached_deck(data["id"])
        finally:
            _mod._oracle_type_index = orig
        self.assertIsNotNone(result)
        self.assertEqual(result["deck_id"], data["id"])

    def test_get_cached_deck_miss(self):
        result = get_cached_deck("nonexistent-deck-id-xyz")
        self.assertIsNone(result)

    def test_get_cached_deck_returns_cards(self):
        data = self._load_fixture()
        cache_deck(data["id"], data)
        import web_enrichment.moxfield as _mod
        orig = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX
        try:
            result = get_cached_deck(data["id"])
        finally:
            _mod._oracle_type_index = orig
        self.assertIn("cards", result)
        self.assertGreater(len(result["cards"]), 0)

    def test_get_cached_deck_excludes_basics(self):
        data = self._load_fixture()
        cache_deck(data["id"], data)
        import web_enrichment.moxfield as _mod
        orig = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX
        try:
            result = get_cached_deck(data["id"])
        finally:
            _mod._oracle_type_index = orig
        names = {c["name"] for c in result["cards"]}
        self.assertNotIn("Forest", names)

    def test_get_cached_deck_printing_mode_any(self):
        data = self._load_fixture()
        cache_deck(data["id"], data)
        import web_enrichment.moxfield as _mod
        orig = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX
        try:
            result = get_cached_deck(data["id"], printing_mode=PRINTING_MODE_ANY)
        finally:
            _mod._oracle_type_index = orig
        self.assertEqual(result["printing_mode"], PRINTING_MODE_ANY)

    def test_get_cached_deck_printing_mode_exact(self):
        data = self._load_fixture()
        cache_deck(data["id"], data)
        import web_enrichment.moxfield as _mod
        orig = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX
        try:
            result = get_cached_deck(data["id"], printing_mode=PRINTING_MODE_EXACT)
        finally:
            _mod._oracle_type_index = orig
        self.assertEqual(result["printing_mode"], PRINTING_MODE_EXACT)


# ---------------------------------------------------------------------------
# Test: cache_wishlist and get_cached_wishlist
# ---------------------------------------------------------------------------

class TestWishlistCache(_DBTestCase):

    def _make_cards(self):
        return [
            {
                "oracle_id":        SOL_RING_OID,
                "name":             "Sol Ring",
                "quantity":         1,
                "set":              "clb",
                "collector_number": "472",
                "scryfall_id":      "aaaa",
            },
            {
                "oracle_id":        BOLT_OID,
                "name":             "Lightning Bolt",
                "quantity":         2,
                "set":              "m11",
                "collector_number": "145",
                "scryfall_id":      "bbbb",
            },
        ]

    def test_cache_wishlist_writes_rows(self):
        cards = self._make_cards()
        cache_wishlist("testuser", cards)
        rows = _direct_query(
            self.db_path,
            "SELECT * FROM moxfield_wishlists WHERE username = ?",
            ("testuser",),
        )
        self.assertEqual(len(rows), 2)

    def test_get_cached_wishlist_hit(self):
        cards = self._make_cards()
        cache_wishlist("testuser", cards)
        result = get_cached_wishlist("testuser")
        self.assertIsNotNone(result)
        self.assertEqual(result["username"], "testuser")
        self.assertEqual(len(result["cards"]), 2)

    def test_get_cached_wishlist_miss(self):
        result = get_cached_wishlist("nobody")
        self.assertIsNone(result)

    def test_cache_wishlist_replaces_atomically(self):
        """Calling cache_wishlist twice: second call fully replaces first."""
        original = self._make_cards()
        cache_wishlist("testuser", original)

        # Different card list
        new_cards = [
            {
                "oracle_id":        ATRAXA_OID,
                "name":             "Atraxa",
                "quantity":         1,
                "set":              "ncc",
                "collector_number": "1",
                "scryfall_id":      "cccc",
            }
        ]
        cache_wishlist("testuser", new_cards)
        result = get_cached_wishlist("testuser")
        self.assertIsNotNone(result)
        self.assertEqual(len(result["cards"]), 1)
        self.assertEqual(result["cards"][0]["oracle_id"], ATRAXA_OID)
        # Original cards must be gone.
        oracle_ids = {c["oracle_id"] for c in result["cards"]}
        self.assertNotIn(SOL_RING_OID, oracle_ids)

    def test_cache_wishlist_empty_list_clears_existing(self):
        cache_wishlist("testuser", self._make_cards())
        cache_wishlist("testuser", [])
        result = get_cached_wishlist("testuser")
        self.assertIsNone(result)  # no rows → cache miss

    def test_cache_wishlist_multiple_users_independent(self):
        cache_wishlist("alice", self._make_cards())
        cache_wishlist("bob", [
            {
                "oracle_id":        ATRAXA_OID,
                "name":             "Atraxa",
                "quantity":         1,
                "set":              "ncc",
                "collector_number": "1",
                "scryfall_id":      "xxxx",
            }
        ])
        alice = get_cached_wishlist("alice")
        bob = get_cached_wishlist("bob")
        self.assertEqual(len(alice["cards"]), 2)
        self.assertEqual(len(bob["cards"]), 1)


# ---------------------------------------------------------------------------
# Test: is_in_deck
# ---------------------------------------------------------------------------

class TestIsInDeck(_DBTestCase):

    def setUp(self):
        super().setUp()
        self.fixture = _load_deck_fixture()
        import web_enrichment.moxfield as _mod
        self._orig = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX

    def tearDown(self):
        import web_enrichment.moxfield as _mod
        _mod._oracle_type_index = self._orig
        super().tearDown()

    def test_is_in_deck_any_mode_match(self):
        cache_deck(self.fixture["id"], self.fixture)
        self.assertTrue(
            is_in_deck(self.fixture["id"], SOL_RING_OID,
                       printing_mode=PRINTING_MODE_ANY)
        )

    def test_is_in_deck_any_mode_no_match(self):
        cache_deck(self.fixture["id"], self.fixture)
        self.assertFalse(
            is_in_deck(self.fixture["id"], "00000000-not-there",
                       printing_mode=PRINTING_MODE_ANY)
        )

    def test_is_in_deck_exact_match_set_cn(self):
        cache_deck(self.fixture["id"], self.fixture)
        self.assertTrue(
            is_in_deck(self.fixture["id"], SOL_RING_OID,
                       printing_mode=PRINTING_MODE_EXACT,
                       set_code="clb", collector_number="472")
        )

    def test_is_in_deck_exact_wrong_set_no_match(self):
        cache_deck(self.fixture["id"], self.fixture)
        self.assertFalse(
            is_in_deck(self.fixture["id"], SOL_RING_OID,
                       printing_mode=PRINTING_MODE_EXACT,
                       set_code="m11", collector_number="472")
        )

    def test_is_in_deck_basic_land_excluded(self):
        """Basic lands should not appear even after cache_deck."""
        cache_deck(self.fixture["id"], self.fixture)
        # Forest is oracle_id ce7b1f32-... and type_line "Basic Land — Forest"
        self.assertFalse(
            is_in_deck(self.fixture["id"], FOREST_OID,
                       printing_mode=PRINTING_MODE_ANY)
        )

    def test_is_in_deck_uncached_returns_false(self):
        self.assertFalse(
            is_in_deck("no-such-deck", SOL_RING_OID,
                       printing_mode=PRINTING_MODE_ANY)
        )


# ---------------------------------------------------------------------------
# Test: is_in_wishlist
# ---------------------------------------------------------------------------

class TestIsInWishlist(_DBTestCase):

    def setUp(self):
        super().setUp()
        self.cards = [
            {
                "oracle_id":        SOL_RING_OID,
                "name":             "Sol Ring",
                "quantity":         1,
                "set":              "clb",
                "collector_number": "472",
                "scryfall_id":      "",
            },
        ]
        cache_wishlist("testuser", self.cards)

    def test_is_in_wishlist_any_mode_match(self):
        self.assertTrue(
            is_in_wishlist("testuser", SOL_RING_OID,
                           printing_mode=PRINTING_MODE_ANY)
        )

    def test_is_in_wishlist_any_mode_no_match(self):
        self.assertFalse(
            is_in_wishlist("testuser", "00000000-not-there",
                           printing_mode=PRINTING_MODE_ANY)
        )

    def test_is_in_wishlist_exact_match(self):
        self.assertTrue(
            is_in_wishlist("testuser", SOL_RING_OID,
                           printing_mode=PRINTING_MODE_EXACT,
                           set_code="clb", collector_number="472")
        )

    def test_is_in_wishlist_exact_wrong_cn(self):
        self.assertFalse(
            is_in_wishlist("testuser", SOL_RING_OID,
                           printing_mode=PRINTING_MODE_EXACT,
                           set_code="clb", collector_number="999")
        )

    def test_is_in_wishlist_unknown_user(self):
        self.assertFalse(
            is_in_wishlist("nobody", SOL_RING_OID,
                           printing_mode=PRINTING_MODE_ANY)
        )


# ---------------------------------------------------------------------------
# Test: MoxfieldSource.import_deck — happy path with mocked HTTP
# ---------------------------------------------------------------------------

class TestMoxfieldSourceImport(_DBTestCase):

    def setUp(self):
        super().setUp()
        self.source = MoxfieldSource()
        self.fixture = _load_deck_fixture()
        import web_enrichment.moxfield as _mod
        self._orig_idx = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX

    def tearDown(self):
        import web_enrichment.moxfield as _mod
        _mod._oracle_type_index = self._orig_idx
        super().tearDown()

    def _mock_fetch(self, data):
        return patch("web_enrichment.moxfield.fetch_deck", return_value=data)

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

    def test_import_deck_card_count_excludes_basics(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_deck(self.fixture["id"])
        # Fixture has Sol Ring (1), Lightning Bolt (1), Forest (3, excluded),
        # Atraxa (1 commander).  After basic exclusion: 1+1+1 = 3.
        self.assertEqual(result["card_count"], 3)

    def test_import_deck_caches_result(self):
        with self._mock_fetch(self.fixture):
            self.source.import_deck(self.fixture["id"])
        rows = _direct_query(
            self.db_path,
            "SELECT * FROM moxfield_decks WHERE deck_id = ?",
            (self.fixture["id"],),
        )
        self.assertEqual(len(rows), 1)

    def test_import_deck_cache_hit_skips_fetch(self):
        with self._mock_fetch(self.fixture):
            self.source.import_deck(self.fixture["id"])
        with patch("web_enrichment.moxfield.fetch_deck",
                   side_effect=AssertionError("fetch should not be called")):
            result = self.source.import_deck(self.fixture["id"], use_cache=True)
        self.assertEqual(result["deck_id"], self.fixture["id"])

    def test_import_deck_use_cache_false_refetches(self):
        with self._mock_fetch(self.fixture):
            self.source.import_deck(self.fixture["id"])
        call_count = [0]

        def counting_fetch(deck_id):
            call_count[0] += 1
            return self.fixture

        with patch("web_enrichment.moxfield.fetch_deck",
                   side_effect=counting_fetch):
            self.source.import_deck(self.fixture["id"], use_cache=False)
        self.assertEqual(call_count[0], 1)

    def test_import_deck_idempotent(self):
        with self._mock_fetch(self.fixture):
            r1 = self.source.import_deck(self.fixture["id"], use_cache=False)
        with self._mock_fetch(self.fixture):
            r2 = self.source.import_deck(self.fixture["id"], use_cache=False)
        self.assertEqual(r1["deck_id"], r2["deck_id"])
        self.assertEqual(r1["card_count"], r2["card_count"])
        rows = _direct_query(
            self.db_path,
            "SELECT COUNT(*) AS n FROM moxfield_decks WHERE deck_id = ?",
            (self.fixture["id"],),
        )
        self.assertEqual(rows[0]["n"], 1)

    def test_import_deck_bad_url_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.source.import_deck("https://archidekt.com/decks/12345")

    def test_import_deck_empty_deck_id_raises(self):
        with self.assertRaises(ValueError):
            self.source.import_deck("")

    def test_import_deck_printing_mode_any(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_deck(
                self.fixture["id"], printing_mode=PRINTING_MODE_ANY
            )
        self.assertEqual(result["printing_mode"], PRINTING_MODE_ANY)

    def test_import_deck_printing_mode_exact(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_deck(
                self.fixture["id"], printing_mode=PRINTING_MODE_EXACT
            )
        self.assertEqual(result["printing_mode"], PRINTING_MODE_EXACT)
        # All cards with set+cn should have exact mode.
        for c in result["cards"]:
            if c["set"] and c["collector_number"]:
                self.assertEqual(c["printing_mode"], PRINTING_MODE_EXACT)

    def test_import_deck_invalid_printing_mode_raises(self):
        with self.assertRaises(ValueError):
            self.source.import_deck(self.fixture["id"], printing_mode="badmode")

    def test_import_deck_response_has_warnings_key(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_deck(self.fixture["id"])
        self.assertIn("warnings", result)
        self.assertIsInstance(result["warnings"], list)

    def test_import_deck_basic_lands_absent_from_cards(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_deck(self.fixture["id"])
        names = {c["name"] for c in result["cards"]}
        self.assertNotIn("Forest", names)
        self.assertNotIn("Island", names)
        self.assertNotIn("Plains", names)
        self.assertNotIn("Mountain", names)
        self.assertNotIn("Swamp", names)
        self.assertNotIn("Wastes", names)


# ---------------------------------------------------------------------------
# Test: MoxfieldSource.import_wishlist
# ---------------------------------------------------------------------------

class TestMoxfieldSourceWishlist(_DBTestCase):

    def setUp(self):
        super().setUp()
        self.source = MoxfieldSource()
        self.fixture = _load_wishlist_fixture()
        import web_enrichment.moxfield as _mod
        self._orig_idx = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX

    def tearDown(self):
        import web_enrichment.moxfield as _mod
        _mod._oracle_type_index = self._orig_idx
        super().tearDown()

    def _mock_fetch(self, data):
        return patch("web_enrichment.moxfield.fetch_wishlist", return_value=data)

    def test_import_wishlist_returns_username(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_wishlist("testuser")
        self.assertEqual(result["username"], "testuser")

    def test_import_wishlist_card_count_excludes_basics(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_wishlist("testuser")
        # Fixture has: Sol Ring (1), Lightning Bolt (2), Forest (4, excl.),
        # Wastes (1, excl.), Atraxa (1), Mysterious Card (1) = 5 included.
        self.assertEqual(result["card_count"], 5)

    def test_import_wishlist_basic_lands_absent(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_wishlist("testuser")
        names = {c["name"] for c in result["cards"]}
        self.assertNotIn("Forest", names)
        self.assertNotIn("Wastes", names)

    def test_import_wishlist_nonbasics_present(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_wishlist("testuser")
        names = {c["name"] for c in result["cards"]}
        self.assertIn("Sol Ring", names)
        self.assertIn("Lightning Bolt", names)
        self.assertIn("Atraxa, Praetors' Voice", names)

    def test_import_wishlist_caches_result(self):
        with self._mock_fetch(self.fixture):
            self.source.import_wishlist("testuser")
        rows = _direct_query(
            self.db_path,
            "SELECT COUNT(*) AS n FROM moxfield_wishlists WHERE username = ?",
            ("testuser",),
        )
        self.assertGreater(rows[0]["n"], 0)

    def test_import_wishlist_cache_hit_skips_fetch(self):
        with self._mock_fetch(self.fixture):
            self.source.import_wishlist("testuser")
        with patch("web_enrichment.moxfield.fetch_wishlist",
                   side_effect=AssertionError("should not fetch on cache hit")):
            result = self.source.import_wishlist("testuser", use_cache=True)
        self.assertEqual(result["username"], "testuser")

    def test_import_wishlist_use_cache_false_refetches(self):
        with self._mock_fetch(self.fixture):
            self.source.import_wishlist("testuser")
        count = [0]

        def counting_fetch(username):
            count[0] += 1
            return self.fixture

        with patch("web_enrichment.moxfield.fetch_wishlist",
                   side_effect=counting_fetch):
            self.source.import_wishlist("testuser", use_cache=False)
        self.assertEqual(count[0], 1)

    def test_import_wishlist_idempotent(self):
        """Two wishlist imports produce identical DB state (row count stable)."""
        with self._mock_fetch(self.fixture):
            self.source.import_wishlist("testuser", use_cache=False)
        with self._mock_fetch(self.fixture):
            self.source.import_wishlist("testuser", use_cache=False)
        rows = _direct_query(
            self.db_path,
            "SELECT COUNT(*) AS n FROM moxfield_wishlists WHERE username = ?",
            ("testuser",),
        )
        # Still same count: second import atomically replaced the first.
        # Count of distinct non-basic cards from fixture: 4 (sol, bolt, atraxa, mysterious)
        self.assertEqual(rows[0]["n"], 4)

    def test_import_wishlist_printing_mode_any(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_wishlist("testuser",
                                                 printing_mode=PRINTING_MODE_ANY)
        self.assertEqual(result["printing_mode"], PRINTING_MODE_ANY)

    def test_import_wishlist_printing_mode_exact(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_wishlist("testuser",
                                                 printing_mode=PRINTING_MODE_EXACT)
        self.assertEqual(result["printing_mode"], PRINTING_MODE_EXACT)

    def test_import_wishlist_exact_fallback_for_missing_set_cn(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_wishlist("testuser",
                                                 printing_mode=PRINTING_MODE_EXACT)
        mystery = next(
            (c for c in result["cards"] if c["name"] == "Mysterious Card"), None
        )
        # Mysterious Card has no set/cn → must fall back to any.
        self.assertIsNotNone(mystery)
        self.assertEqual(mystery["printing_mode"], PRINTING_MODE_ANY)
        # A warning must be present about the fallback.
        self.assertTrue(len(result["warnings"]) > 0)

    def test_import_wishlist_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            self.source.import_wishlist("testuser", printing_mode="bad")

    def test_import_wishlist_empty_username_raises(self):
        with self.assertRaises(ValueError):
            self.source.import_wishlist("")

    def test_import_wishlist_response_has_printing_mode(self):
        with self._mock_fetch(self.fixture):
            result = self.source.import_wishlist("testuser")
        self.assertIn("printing_mode", result)


# ---------------------------------------------------------------------------
# Test: empty response — existing cache not wiped
# ---------------------------------------------------------------------------

class TestEmptyAndMalformedResponses(_DBTestCase):

    def setUp(self):
        super().setUp()
        self.source = MoxfieldSource()
        self.fixture = _load_deck_fixture()
        import web_enrichment.moxfield as _mod
        self._orig_idx = _mod._oracle_type_index
        _mod._oracle_type_index = _FAKE_ORACLE_TYPE_INDEX

    def tearDown(self):
        import web_enrichment.moxfield as _mod
        _mod._oracle_type_index = self._orig_idx
        super().tearDown()

    def test_deck_with_no_mainboard_cards_still_caches(self):
        empty_deck = dict(self.fixture)
        empty_deck["mainboard"] = {}
        empty_deck["commanders"] = {}
        with patch("web_enrichment.moxfield.fetch_deck",
                   return_value=empty_deck):
            result = self.source.import_deck(self.fixture["id"], use_cache=False)
        self.assertEqual(result["card_count"], 0)
        self.assertEqual(result["cards"], [])
        rows = _direct_query(
            self.db_path,
            "SELECT * FROM moxfield_decks WHERE deck_id = ?",
            (self.fixture["id"],),
        )
        self.assertEqual(len(rows), 1)

    def test_malformed_response_missing_required_key_raises(self):
        with patch("web_enrichment.moxfield.fetch_deck",
                   side_effect=ValueError("missing keys: ['format', 'mainboard']")):
            with self.assertRaises(ValueError):
                self.source.import_deck("abc12345def", use_cache=False)

    def test_malformed_response_no_partial_commit(self):
        deck_id = self.fixture["id"]

        def broken_fetch(did):
            raise ValueError("Simulated parse failure")

        with patch("web_enrichment.moxfield.fetch_deck",
                   side_effect=broken_fetch):
            with self.assertRaises(ValueError):
                self.source.import_deck(deck_id, use_cache=False)

        rows = _direct_query(
            self.db_path,
            "SELECT * FROM moxfield_decks WHERE deck_id = ?",
            (deck_id,),
        )
        self.assertEqual(len(rows), 0)

    def test_network_error_propagates(self):
        import httpx
        with patch("web_enrichment.moxfield.fetch_deck",
                   side_effect=httpx.RequestError("timeout")):
            with self.assertRaises(httpx.RequestError):
                self.source.import_deck(self.fixture["id"], use_cache=False)

    def test_wishlist_empty_response_returns_empty_list(self):
        with patch("web_enrichment.moxfield.fetch_wishlist",
                   return_value={}):
            result = self.source.import_wishlist("emptyuser", use_cache=False)
        self.assertEqual(result["card_count"], 0)
        self.assertEqual(result["cards"], [])


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
        source = MagicMock()
        mock_result = MagicMock()
        mock_result.ok = False
        with patch("probes.probe_moxfield.probe", return_value=mock_result):
            source2 = MoxfieldSource()
            result = source2.probe()
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Test: refresh is a no-op stub that still updates sync_metadata
# ---------------------------------------------------------------------------

class TestMoxfieldRefresh(_DBTestCase):

    def test_refresh_returns_success(self):
        source = MoxfieldSource()
        result = source.refresh()
        self.assertTrue(result.success)
        self.assertEqual(result.source, "moxfield")

    def test_refresh_updates_sync_metadata(self):
        source = MoxfieldSource()
        source.refresh()
        rows = _direct_query(
            self.db_path,
            "SELECT * FROM sync_metadata WHERE source = 'moxfield'",
        )
        self.assertEqual(len(rows), 1)

    def test_coverage_report_includes_cached_decks(self):
        source = MoxfieldSource()
        data = _load_deck_fixture()
        cache_deck(data["id"], data)
        report = source.coverage_report()
        self.assertEqual(report["cached_decks"], 1)

    def test_coverage_report_includes_cached_wishlists(self):
        source = MoxfieldSource()
        cache_wishlist("alice", [
            {
                "oracle_id":        SOL_RING_OID,
                "name":             "Sol Ring",
                "quantity":         1,
                "set":              "clb",
                "collector_number": "472",
                "scryfall_id":      "",
            }
        ])
        report = source.coverage_report()
        self.assertEqual(report["cached_wishlists"], 1)


# ---------------------------------------------------------------------------
# Test: snow basic and multi-basic edge cases
# ---------------------------------------------------------------------------

class TestBasicLandEdgeCases(unittest.TestCase):
    """Edge-case tests for basic-land exclusion logic."""

    def setUp(self):
        import web_enrichment.moxfield as _mod
        self._orig = _mod._oracle_type_index
        _mod._oracle_type_index = dict(_FAKE_ORACLE_TYPE_INDEX)
        # Add snow basics.
        _mod._oracle_type_index["snow-island-oid"]   = "Basic Snow Land — Island"
        _mod._oracle_type_index["snow-mountain-oid"] = "Basic Snow Land — Mountain"
        _mod._oracle_type_index["snow-plains-oid"]   = "Basic Snow Land — Plains"
        _mod._oracle_type_index["snow-swamp-oid"]    = "Basic Snow Land — Swamp"

    def tearDown(self):
        import web_enrichment.moxfield as _mod
        _mod._oracle_type_index = self._orig

    def test_snow_island_excluded(self):
        self.assertTrue(is_basic_land("snow-island-oid", "Snow-Covered Island"))

    def test_snow_mountain_excluded(self):
        self.assertTrue(is_basic_land("snow-mountain-oid", "Snow-Covered Mountain"))

    def test_snow_plains_excluded(self):
        self.assertTrue(is_basic_land("snow-plains-oid", "Snow-Covered Plains"))

    def test_snow_swamp_excluded(self):
        self.assertTrue(is_basic_land("snow-swamp-oid", "Snow-Covered Swamp"))

    def test_wastes_excluded(self):
        # Wastes has type_line "Basic Land" (no subtype).
        self.assertTrue(is_basic_land(WASTES_OID, "Wastes"))

    def test_dfc_back_face_basic_front_not_excluded(self):
        """A DFC whose FRONT face oracle_id maps to Sorcery must not be excluded.

        In Moxfield, the oracle_id on a DFC entry is always the front face.
        A Moxfield entry like "Valakut Awakening" (front = Sorcery) should
        not be excluded even though the card also has a back face that is
        a basic land.  We look up by oracle_id (front face) only.
        """
        # "dfc-front-oid" is in the fake index as "Sorcery".
        self.assertFalse(is_basic_land("dfc-front-oid", "DFC Front Face"))


if __name__ == "__main__":
    unittest.main()
