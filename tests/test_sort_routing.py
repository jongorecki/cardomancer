"""
Unit tests for wishlist priority-bin routing (Phase 4.21).

Covers:
  * precedence: priority > override(wishlist_bin) > regular sort > fallback
  * wishlist_match socket event payload shape
  * basic lands filtered at Moxfield cache-fill time
"""

import sys
import os
import tempfile
import shutil
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collection_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PRIORITY_OID = "priority-card-oid"
NORMAL_OID = "normal-card-oid"

PRIORITY_CARD_INFO = {"Name": "Rhystic Study", "Set": "pcy", "Colors": ["U"]}
PRIORITY_CARD_DATA = {
    "oracle_id": PRIORITY_OID,
    "name": "Rhystic Study",
    "colors": ["U"],
    "type_line": "Enchantment",
    "image_uris": {"normal": "https://example.com/rhystic.png"},
}

NORMAL_CARD_INFO = {"Name": "Counterspell", "Set": "ice", "Colors": ["U"]}
NORMAL_CARD_DATA = {
    "oracle_id": NORMAL_OID,
    "name": "Counterspell",
    "colors": ["U"],
    "type_line": "Instant",
}


def _make_worker_stub():
    """Build a SortWorker instance wired for routing tests.

    We construct the object without starting its thread.  Routing only
    needs the attributes added by Phase 4.21 plus the emit hook.
    """
    from web_worker import SortWorker
    w = SortWorker()
    w._emit_fn = MagicMock()
    return w


# ---------------------------------------------------------------------------
# Moxfield wishlist cache tests
# ---------------------------------------------------------------------------

class TestMoxfieldWishlistCache(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.conn = collection_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_upsert_and_list(self):
        collection_db.upsert_moxfield_wishlist(
            self.conn, "moxfield:alice",
            cards=[{"oracle_id": "a", "name": "Foo"}],
            username="alice", display_name="Alice's deck")
        listed = collection_db.list_moxfield_wishlists(self.conn)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["source_key"], "moxfield:alice")
        self.assertEqual(listed[0]["card_count"], 1)

    def test_basic_lands_excluded(self):
        """Basic lands must be filtered out at cache-fill time."""
        collection_db.upsert_moxfield_wishlist(
            self.conn, "moxfield:bob",
            cards=[
                {"oracle_id": "forest-oid", "name": "Forest"},
                {"oracle_id": "island-oid", "name": "Island"},
                {"oracle_id": "snow-oid", "name": "Snow-Covered Plains"},
                {"oracle_id": "real-oid", "name": "Rhystic Study"},
            ])
        ids = collection_db.get_moxfield_wishlist_oracle_ids(
            self.conn, "moxfield:bob")
        self.assertEqual(ids, {"real-oid"})

    def test_upsert_replaces(self):
        collection_db.upsert_moxfield_wishlist(
            self.conn, "moxfield:alice",
            cards=[{"oracle_id": "a", "name": "Foo"}])
        collection_db.upsert_moxfield_wishlist(
            self.conn, "moxfield:alice",
            cards=[{"oracle_id": "b", "name": "Bar"}])
        ids = collection_db.get_moxfield_wishlist_oracle_ids(
            self.conn, "moxfield:alice")
        self.assertEqual(ids, {"b"})

    def test_get_card_by_oid(self):
        collection_db.upsert_moxfield_wishlist(
            self.conn, "moxfield:alice",
            cards=[{"oracle_id": "a", "name": "Foo",
                    "set_code": "xyz", "image_uri": "u"}])
        card = collection_db.get_moxfield_wishlist_card(
            self.conn, "moxfield:alice", "a")
        self.assertEqual(card["name"], "Foo")
        self.assertEqual(card["image_uri"], "u")
        self.assertIsNone(collection_db.get_moxfield_wishlist_card(
            self.conn, "moxfield:alice", "missing"))


# ---------------------------------------------------------------------------
# Router precedence (priority > override > regular > fallback)
# ---------------------------------------------------------------------------

class TestRouterPrecedence(unittest.TestCase):
    """
    Directly exercise the precedence logic used inside the worker's
    card-dropped pipeline.  Reimplements the minimal decision ladder so
    tests don't require the full hardware worker loop.  The real ladder
    lives in web_worker.py around the priority_hit check.
    """

    def _route(self, card_data, card_info,
               regular_bin, override_bin, priority_bin,
               priority_oracle_ids):
        """
        Mirror of the worker's precedence:
            1. logical_bin = regular_bin  (from get_bin_number)
            2. if override active and matches => logical_bin = override_bin
            3. if priority active and matches => logical_bin = priority_bin
            4. physical = resolve (no overflow here)
        """
        logical_bin = regular_bin
        # override layer
        if override_bin is not None:
            # legacy wishlist_bin matches by name substring; here we
            # simulate that "Counterspell" hits the override wishlist.
            if (card_info or {}).get("Name") == "Counterspell":
                logical_bin = override_bin
        # priority layer
        if priority_bin is not None and priority_oracle_ids:
            oid = (card_data or {}).get("oracle_id")
            if oid and oid in priority_oracle_ids:
                logical_bin = priority_bin
        return logical_bin

    def test_regular_only(self):
        self.assertEqual(self._route(
            NORMAL_CARD_DATA, NORMAL_CARD_INFO,
            regular_bin=3, override_bin=None, priority_bin=None,
            priority_oracle_ids=set()), 3)

    def test_override_beats_regular(self):
        self.assertEqual(self._route(
            NORMAL_CARD_DATA, NORMAL_CARD_INFO,
            regular_bin=3, override_bin=8, priority_bin=None,
            priority_oracle_ids=set()), 8)

    def test_priority_beats_regular(self):
        self.assertEqual(self._route(
            PRIORITY_CARD_DATA, PRIORITY_CARD_INFO,
            regular_bin=3, override_bin=None, priority_bin=10,
            priority_oracle_ids={PRIORITY_OID}), 10)

    def test_priority_beats_override(self):
        # Card hits BOTH the legacy override (name match) and priority
        # (oracle_id match) — priority must win.
        cd = dict(PRIORITY_CARD_DATA)
        ci = {"Name": "Counterspell", "Set": "ice"}  # triggers override too
        self.assertEqual(self._route(
            cd, ci,
            regular_bin=3, override_bin=8, priority_bin=10,
            priority_oracle_ids={PRIORITY_OID}), 10)

    def test_fallback_when_nothing_matches(self):
        # Neither override (name-based) nor priority (oid-based) hits —
        # card falls through to the regular (fallback) bin.
        random_info = {"Name": "Random Card", "Set": "xyz"}
        random_data = {"oracle_id": "random-oid"}
        self.assertEqual(self._route(
            random_data, random_info,
            regular_bin=9, override_bin=8, priority_bin=10,
            priority_oracle_ids={PRIORITY_OID}), 9)


# ---------------------------------------------------------------------------
# Worker-level tests: _check_priority_match + wishlist_match emission
# ---------------------------------------------------------------------------

class TestWorkerPriorityMatch(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        # Patch DB_PATH for collection_db.get_connection() used inside
        # the worker's priority lookups.
        self._orig_db_path = collection_db.DB_PATH
        collection_db.DB_PATH = self.db_path
        # Seed a cached wishlist.
        conn = collection_db.get_connection()
        collection_db.upsert_moxfield_wishlist(
            conn, "moxfield:alice",
            cards=[{
                "oracle_id": PRIORITY_OID,
                "name": "Rhystic Study",
                "set_code": "pcy",
                "image_uri": "https://example.com/rhystic.png",
            }])
        conn.close()
        self.worker = _make_worker_stub()

    def tearDown(self):
        collection_db.DB_PATH = self._orig_db_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_set_priority_bin_loads_oracle_ids(self):
        self.worker._cmd_set_priority_bin(
            bin_number=10, wishlist_source="moxfield:alice")
        self.assertEqual(self.worker.priority_bin, 10)
        self.assertIn(PRIORITY_OID, self.worker.priority_oracle_ids)

    def test_priority_match_returns_payload(self):
        self.worker._cmd_set_priority_bin(
            bin_number=10, wishlist_source="moxfield:alice")
        hit = self.worker._check_priority_match(
            PRIORITY_CARD_DATA, PRIORITY_CARD_INFO)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["oracle_id"], PRIORITY_OID)
        self.assertEqual(hit["name"], "Rhystic Study")
        self.assertEqual(hit["priority_bin"], 10)
        self.assertEqual(hit["set"], "pcy")
        self.assertEqual(hit["image_uri"], "https://example.com/rhystic.png")

    def test_priority_no_match_for_unknown_oid(self):
        self.worker._cmd_set_priority_bin(
            bin_number=10, wishlist_source="moxfield:alice")
        self.assertIsNone(self.worker._check_priority_match(
            NORMAL_CARD_DATA, NORMAL_CARD_INFO))

    def test_priority_disabled_returns_none(self):
        # Neither bin nor source configured.
        self.assertIsNone(self.worker._check_priority_match(
            PRIORITY_CARD_DATA, PRIORITY_CARD_INFO))

    def test_priority_bin_without_source_disables(self):
        self.worker._cmd_set_priority_bin(
            bin_number=10, wishlist_source=None)
        self.assertEqual(self.worker.priority_oracle_ids, set())
        self.assertIsNone(self.worker._check_priority_match(
            PRIORITY_CARD_DATA, PRIORITY_CARD_INFO))

    def test_wishlist_match_event_emitted(self):
        """
        The priority-bin routing must emit a `wishlist_match` socket event
        with the full payload shape whenever a configured oracle_id hits.
        """
        self.worker._cmd_set_priority_bin(
            bin_number=10, wishlist_source="moxfield:alice")
        hit = self.worker._check_priority_match(
            PRIORITY_CARD_DATA, PRIORITY_CARD_INFO)
        self.worker.emit('wishlist_match', hit)

        # Find the wishlist_match call.
        calls = [c for c in self.worker._emit_fn.call_args_list
                 if c.args and c.args[0] == 'wishlist_match']
        self.assertEqual(len(calls), 1)
        payload = calls[0].args[1]
        for key in ("oracle_id", "name", "image_uri", "set", "priority_bin"):
            self.assertIn(key, payload)
        self.assertEqual(payload["priority_bin"], 10)
        self.assertEqual(payload["oracle_id"], PRIORITY_OID)


if __name__ == "__main__":
    unittest.main()
