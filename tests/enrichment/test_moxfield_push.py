# tests/enrichment/test_moxfield_push.py
# ---------------------------------------------------------------------------
# Unit tests for web_enrichment/moxfield_push.py.
#
# All HTTP calls are mocked — no live Moxfield traffic.
# Uses the DB_PATH redirect pattern (NOT patching get_connection) because the
# module calls conn.close() in finally blocks.
#
# Coverage:
#   compute_collection_diff:
#     - empty-on-both
#     - adds-only
#     - removes-only
#     - qty-updates only
#     - mixed (adds + removes + updates)
#     - basic-land exclusion (Forest, basic land types)
#     - foil_qty pass-through on adds
#     - idempotency: calling twice with same inputs produces identical result
#     - zero-qty items treated as absent
#
#   Auth / token refresh:
#     - get_bearer() returns env token on first call
#     - get_bearer(force_refresh=True) calls _do_refresh
#     - 401 response triggers refresh-and-retry exactly once
#
#   push_deck_diff:
#     - happy path: builds PATCH body and sends
#     - empty diff: returns no_changes sentinel, does NOT call PATCH
#     - removes without card_id are skipped
#
#   get_moxfield_deck_cards:
#     - parses mainboard + commanders boards
#     - basic lands NOT excluded here (exclusion is in compute_collection_diff)
#
#   get_local_inventory:
#     - reads from collection.db (temp DB)
#     - aggregates qty by oracle_id
#     - skips rows without oracle_id
#
#   sync_manifests helpers:
#     - update_manifest upserts correctly
#     - get_manifest reads back
#     - deleting a manifest entry removes it
#     - manifest key format: "moxfield:<deck_id>"
#
#   run_push:
#     - dry-run returns diff preview without calling PATCH
#     - commit=False default: dry-run
#     - commit=True sends PATCH and updates sync_manifests
#     - Moxfield error (4xx) surfaces as RuntimeError with HTTP status
#     - no-changes case: committed=False, total_changes=0
#
#   Integration: auth retry (401 → refresh → 200)
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import collection_db
from web_enrichment.moxfield_push import (
    _build_patch_body,
    _parse_remote_cards,
    compute_collection_diff,
    delete_manifest_entry,
    get_local_inventory,
    get_manifest,
    is_basic_land,
    push_deck_diff,
    run_push,
    update_manifest,
)

# ---------------------------------------------------------------------------
# Oracle IDs matching the fixture deck_sample.json
# ---------------------------------------------------------------------------
SOL_RING_OID   = "a2e0e217-6a60-4c74-a350-2f099b8e6f00"
BOLT_OID       = "8b4d282e-06fb-4774-a657-5d4a9a41b52e"
FOREST_OID     = "ce7b1f32-a1ec-4fe3-bb46-7a7c79c7ad61"
ATRAXA_OID     = "21935e54-c2a8-4756-8547-1ece5b07d0fa"
COUNTERSPELL_OID = "aab57e45-0000-0000-0000-000000000001"  # local-only
WASTES_OID     = "b2c6aa39-2d21-4f2e-b5ca-c0ccb538f9d4"

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moxfield"

# ---------------------------------------------------------------------------
# Fake oracle type index injected via module-level patch
# ---------------------------------------------------------------------------
FAKE_TYPE_INDEX = {
    SOL_RING_OID:     "Artifact",
    BOLT_OID:         "Instant",
    FOREST_OID:       "Basic Land — Forest",
    ATRAXA_OID:       "Legendary Creature — Phyrexian Angel Horror",
    COUNTERSPELL_OID: "Instant",
    WASTES_OID:       "Basic Land",
}


def _inject_type_index():
    """Patch the module-level oracle type index cache."""
    import web_enrichment.moxfield_push as mp
    mp._oracle_type_index_cache = FAKE_TYPE_INDEX


def _clear_type_index():
    import web_enrichment.moxfield_push as mp
    mp._oracle_type_index_cache = None


def _clear_bearer_cache():
    import web_enrichment.moxfield_push as mp
    mp._bearer_cache = None
    mp._refresh_cache = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_local(**oracle_qtys) -> dict[str, dict]:
    """Build a local_inventory dict from {name: (oracle_id, qty)} pairs."""
    result: dict[str, dict] = {}
    for name, (oid, qty) in oracle_qtys.items():
        result[oid] = {"qty": qty, "foil_qty": 0, "name": name}
    return result


def _make_remote(**oracle_qtys) -> dict[str, dict]:
    """Build a remote_deck_cards dict from {name: (oracle_id, qty, card_id)} tuples."""
    result: dict[str, dict] = {}
    for name, (oid, qty, cid) in oracle_qtys.items():
        result[oid] = {"qty": qty, "card_id": cid, "name": name}
    return result


# ---------------------------------------------------------------------------
# 1. compute_collection_diff tests
# ---------------------------------------------------------------------------

class TestComputeCollectionDiff(unittest.TestCase):

    def setUp(self):
        _inject_type_index()

    def tearDown(self):
        _clear_type_index()

    def test_empty_both_returns_no_changes(self):
        adds, removes, updates = compute_collection_diff({}, {})
        self.assertEqual(adds, [])
        self.assertEqual(removes, [])
        self.assertEqual(updates, [])

    def test_adds_only(self):
        """Cards in local but not remote → adds list."""
        local = {SOL_RING_OID: {"qty": 1, "foil_qty": 0, "name": "Sol Ring"}}
        adds, removes, updates = compute_collection_diff(local, {})
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["oracle_id"], SOL_RING_OID)
        self.assertEqual(adds[0]["qty"], 1)
        self.assertEqual(removes, [])
        self.assertEqual(updates, [])

    def test_removes_only(self):
        """Cards in remote but not local → removes list."""
        remote = {BOLT_OID: {"qty": 2, "card_id": "uuid-bolt-001", "name": "Lightning Bolt"}}
        adds, removes, updates = compute_collection_diff({}, remote)
        self.assertEqual(len(removes), 1)
        self.assertEqual(removes[0]["oracle_id"], BOLT_OID)
        self.assertEqual(removes[0]["card_id"], "uuid-bolt-001")
        self.assertEqual(adds, [])
        self.assertEqual(updates, [])

    def test_qty_update(self):
        """Same oracle_id, different qty → updates list."""
        local = {SOL_RING_OID: {"qty": 3, "foil_qty": 0, "name": "Sol Ring"}}
        remote = {SOL_RING_OID: {"qty": 1, "card_id": "uuid-sol-001", "name": "Sol Ring"}}
        adds, removes, updates = compute_collection_diff(local, remote)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["oracle_id"], SOL_RING_OID)
        self.assertEqual(updates[0]["local_qty"], 3)
        self.assertEqual(updates[0]["remote_qty"], 1)
        self.assertEqual(adds, [])
        self.assertEqual(removes, [])

    def test_mixed_diff(self):
        """Mixed: one add, one remove, one update."""
        local = {
            SOL_RING_OID: {"qty": 2, "foil_qty": 0, "name": "Sol Ring"},
            COUNTERSPELL_OID: {"qty": 1, "foil_qty": 0, "name": "Counterspell"},
        }
        remote = {
            SOL_RING_OID: {"qty": 1, "card_id": "uuid-sol-001", "name": "Sol Ring"},
            BOLT_OID: {"qty": 2, "card_id": "uuid-bolt-001", "name": "Lightning Bolt"},
        }
        adds, removes, updates = compute_collection_diff(local, remote)
        add_oids = {a["oracle_id"] for a in adds}
        remove_oids = {r["oracle_id"] for r in removes}
        update_oids = {u["oracle_id"] for u in updates}
        self.assertIn(COUNTERSPELL_OID, add_oids)
        self.assertIn(BOLT_OID, remove_oids)
        self.assertIn(SOL_RING_OID, update_oids)

    def test_basic_land_excluded_from_adds(self):
        """Forest (Basic Land) must never appear in adds."""
        local = {
            FOREST_OID: {"qty": 5, "foil_qty": 0, "name": "Forest"},
            SOL_RING_OID: {"qty": 1, "foil_qty": 0, "name": "Sol Ring"},
        }
        adds, removes, updates = compute_collection_diff(local, {})
        add_oids = {a["oracle_id"] for a in adds}
        self.assertNotIn(FOREST_OID, add_oids)
        self.assertIn(SOL_RING_OID, add_oids)

    def test_basic_land_excluded_from_removes(self):
        """Forest in remote but not local must not appear in removes."""
        remote = {
            FOREST_OID: {"qty": 3, "card_id": "uuid-forest", "name": "Forest"},
        }
        adds, removes, updates = compute_collection_diff({}, remote)
        self.assertEqual(removes, [])

    def test_basic_land_excluded_from_updates(self):
        """Forest with mismatched qty must not appear in updates."""
        local = {FOREST_OID: {"qty": 5, "foil_qty": 0, "name": "Forest"}}
        remote = {FOREST_OID: {"qty": 1, "card_id": "uuid-forest", "name": "Forest"}}
        adds, removes, updates = compute_collection_diff(local, remote)
        self.assertEqual(updates, [])

    def test_wastes_basic_land_excluded(self):
        """Wastes (Basic Land with no subtype) also excluded."""
        local = {WASTES_OID: {"qty": 2, "foil_qty": 0, "name": "Wastes"}}
        adds, removes, updates = compute_collection_diff(local, {})
        self.assertEqual(adds, [])

    def test_zero_qty_local_treated_as_absent(self):
        """Local qty==0 should not appear in adds."""
        local = {SOL_RING_OID: {"qty": 0, "foil_qty": 0, "name": "Sol Ring"}}
        adds, removes, updates = compute_collection_diff(local, {})
        self.assertEqual(adds, [])

    def test_zero_qty_remote_treated_as_absent(self):
        """Remote qty==0 should not appear in removes; should appear in adds if local > 0."""
        local = {SOL_RING_OID: {"qty": 1, "foil_qty": 0, "name": "Sol Ring"}}
        remote = {SOL_RING_OID: {"qty": 0, "card_id": "uuid-sol", "name": "Sol Ring"}}
        adds, removes, updates = compute_collection_diff(local, remote)
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["oracle_id"], SOL_RING_OID)
        self.assertEqual(removes, [])

    def test_foil_qty_passed_through_on_adds(self):
        """foil_qty from local should be present on add entries."""
        local = {SOL_RING_OID: {"qty": 2, "foil_qty": 1, "name": "Sol Ring"}}
        adds, _, _ = compute_collection_diff(local, {})
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["foil_qty"], 1)

    def test_idempotency(self):
        """Calling twice with same inputs produces identical result."""
        local = {
            SOL_RING_OID: {"qty": 2, "foil_qty": 0, "name": "Sol Ring"},
            COUNTERSPELL_OID: {"qty": 1, "foil_qty": 0, "name": "Counterspell"},
        }
        remote = {
            SOL_RING_OID: {"qty": 1, "card_id": "uuid-sol", "name": "Sol Ring"},
            BOLT_OID: {"qty": 2, "card_id": "uuid-bolt", "name": "Lightning Bolt"},
        }
        r1 = compute_collection_diff(local, remote)
        r2 = compute_collection_diff(local, remote)
        self.assertEqual(len(r1[0]), len(r2[0]))  # adds
        self.assertEqual(len(r1[1]), len(r2[1]))  # removes
        self.assertEqual(len(r1[2]), len(r2[2]))  # updates

    def test_same_qty_produces_no_update(self):
        """Same qty in local and remote → no update entry."""
        local = {SOL_RING_OID: {"qty": 1, "foil_qty": 0, "name": "Sol Ring"}}
        remote = {SOL_RING_OID: {"qty": 1, "card_id": "uuid-sol", "name": "Sol Ring"}}
        adds, removes, updates = compute_collection_diff(local, remote)
        self.assertEqual(adds, [])
        self.assertEqual(removes, [])
        self.assertEqual(updates, [])


# ---------------------------------------------------------------------------
# 2. is_basic_land tests
# ---------------------------------------------------------------------------

class TestIsBasicLand(unittest.TestCase):

    def setUp(self):
        _inject_type_index()

    def tearDown(self):
        _clear_type_index()

    def test_forest_is_basic(self):
        self.assertTrue(is_basic_land(FOREST_OID))

    def test_wastes_is_basic(self):
        self.assertTrue(is_basic_land(WASTES_OID))

    def test_sol_ring_not_basic(self):
        self.assertFalse(is_basic_land(SOL_RING_OID))

    def test_unknown_oracle_id_returns_false(self):
        """Unknown oracle_id should not be excluded (fail-open)."""
        self.assertFalse(is_basic_land("unknown-oracle-id-xyz"))


# ---------------------------------------------------------------------------
# 3. _parse_remote_cards tests
# ---------------------------------------------------------------------------

class TestParseRemoteCards(unittest.TestCase):

    def test_parses_mainboard_and_commanders(self):
        fixture = json.loads(
            (FIXTURES / "deck_sample.json").read_text(encoding="utf-8")
        )
        result = _parse_remote_cards(fixture)
        # Should have Sol Ring, Lightning Bolt, Forest, Atraxa
        self.assertIn(SOL_RING_OID, result)
        self.assertIn(BOLT_OID, result)
        self.assertIn(FOREST_OID, result)
        self.assertIn(ATRAXA_OID, result)

    def test_qty_aggregated_across_boards(self):
        """If oracle_id appears in multiple boards, qtys are summed."""
        data = {
            "mainboard": {
                "uuid-a": {
                    "quantity": 1,
                    "card": {"oracle_id": SOL_RING_OID, "name": "Sol Ring"}
                },
            },
            "commanders": {
                "uuid-b": {
                    "quantity": 1,
                    "card": {"oracle_id": SOL_RING_OID, "name": "Sol Ring"}
                },
            },
        }
        result = _parse_remote_cards(data)
        self.assertEqual(result[SOL_RING_OID]["qty"], 2)

    def test_missing_oracle_id_skipped(self):
        data = {
            "mainboard": {
                "uuid-x": {
                    "quantity": 1,
                    "card": {"name": "Some Card"},  # no oracle_id
                },
            },
        }
        result = _parse_remote_cards(data)
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# 4. get_local_inventory tests (uses temp collection.db)
# ---------------------------------------------------------------------------

class TestGetLocalInventory(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mxpush_test_")
        self.db_path = os.path.join(self.tmpdir, "collection.db")
        self.conn = collection_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_inventory(self, name, oracle_id, qty, foil_qty=0,
                          set_code="lea", cn="1"):
        self.conn.execute(
            """INSERT INTO inventory
               (name, set_code, collector_number, oracle_id, quantity,
                foil_quantity, first_scanned, last_scanned)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            (name, set_code, cn, oracle_id, qty, foil_qty),
        )
        self.conn.commit()

    def test_empty_inventory_returns_empty(self):
        result = get_local_inventory(self.conn)
        self.assertEqual(result, {})

    def test_single_card(self):
        self._insert_inventory("Sol Ring", SOL_RING_OID, 2)
        result = get_local_inventory(self.conn)
        self.assertIn(SOL_RING_OID, result)
        self.assertEqual(result[SOL_RING_OID]["qty"], 2)

    def test_multiple_rows_same_oracle_id_aggregated(self):
        """Multiple inventory rows for same oracle_id should sum qty."""
        self._insert_inventory("Sol Ring", SOL_RING_OID, 1, set_code="lea", cn="1")
        self._insert_inventory("Sol Ring", SOL_RING_OID, 2, set_code="clb", cn="472")
        result = get_local_inventory(self.conn)
        self.assertEqual(result[SOL_RING_OID]["qty"], 3)

    def test_row_without_oracle_id_skipped(self):
        self.conn.execute(
            """INSERT INTO inventory
               (name, set_code, collector_number, oracle_id, quantity,
                first_scanned, last_scanned)
               VALUES (?, ?, ?, NULL, ?, datetime('now'), datetime('now'))""",
            ("Unknown Card", "lea", "999", 1),
        )
        self.conn.commit()
        result = get_local_inventory(self.conn)
        # Should return empty (no oracle_id)
        self.assertFalse(any(oid is None for oid in result))

    def test_foil_qty_included(self):
        self._insert_inventory("Sol Ring", SOL_RING_OID, 1, foil_qty=3)
        result = get_local_inventory(self.conn)
        self.assertEqual(result[SOL_RING_OID]["foil_qty"], 3)


# ---------------------------------------------------------------------------
# 5. sync_manifests / manifest helpers (uses temp collection.db)
# ---------------------------------------------------------------------------

class TestSyncManifests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mxmanifest_test_")
        self.db_path = os.path.join(self.tmpdir, "collection.db")
        self.conn = collection_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_manifest_empty(self):
        result = get_manifest(self.conn, "moxfield:testdeck")
        self.assertEqual(result, {})

    def test_update_and_read_manifest(self):
        update_manifest(self.conn, "moxfield:testdeck",
                        {SOL_RING_OID: 2, BOLT_OID: 1},
                        {SOL_RING_OID: 1})
        result = get_manifest(self.conn, "moxfield:testdeck")
        self.assertIn(SOL_RING_OID, result)
        self.assertEqual(result[SOL_RING_OID]["qty"], 2)
        self.assertEqual(result[SOL_RING_OID]["foil_qty"], 1)
        self.assertIn(BOLT_OID, result)

    def test_update_is_idempotent(self):
        """Calling update_manifest twice with the same data yields one row."""
        update_manifest(self.conn, "moxfield:deck1", {SOL_RING_OID: 2})
        update_manifest(self.conn, "moxfield:deck1", {SOL_RING_OID: 2})
        rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM sync_manifests WHERE target=? AND oracle_id=?",
            ("moxfield:deck1", SOL_RING_OID),
        ).fetchone()
        self.assertEqual(rows["c"], 1)

    def test_update_overwrites_previous_qty(self):
        update_manifest(self.conn, "moxfield:deck1", {SOL_RING_OID: 1})
        update_manifest(self.conn, "moxfield:deck1", {SOL_RING_OID: 5})
        result = get_manifest(self.conn, "moxfield:deck1")
        self.assertEqual(result[SOL_RING_OID]["qty"], 5)

    def test_zero_qty_removes_entry(self):
        """Updating to qty==0 should remove the entry."""
        update_manifest(self.conn, "moxfield:deck1", {SOL_RING_OID: 2})
        update_manifest(self.conn, "moxfield:deck1", {SOL_RING_OID: 0})
        result = get_manifest(self.conn, "moxfield:deck1")
        self.assertNotIn(SOL_RING_OID, result)

    def test_delete_manifest_entry(self):
        update_manifest(self.conn, "moxfield:deck1", {SOL_RING_OID: 1})
        delete_manifest_entry(self.conn, "moxfield:deck1", SOL_RING_OID)
        result = get_manifest(self.conn, "moxfield:deck1")
        self.assertNotIn(SOL_RING_OID, result)

    def test_separate_targets_do_not_interfere(self):
        update_manifest(self.conn, "moxfield:deck1", {SOL_RING_OID: 1})
        update_manifest(self.conn, "moxfield:deck2", {BOLT_OID: 2})
        r1 = get_manifest(self.conn, "moxfield:deck1")
        r2 = get_manifest(self.conn, "moxfield:deck2")
        self.assertIn(SOL_RING_OID, r1)
        self.assertNotIn(BOLT_OID, r1)
        self.assertIn(BOLT_OID, r2)
        self.assertNotIn(SOL_RING_OID, r2)


# ---------------------------------------------------------------------------
# 6. _build_patch_body tests
# ---------------------------------------------------------------------------

class TestBuildPatchBody(unittest.TestCase):

    def test_empty_diff_produces_empty_lists(self):
        body = _build_patch_body([], [], [], {})
        self.assertEqual(body["addCards"], [])
        self.assertEqual(body["deleteCards"], [])
        self.assertEqual(body["updateCards"], [])

    def test_add_entry_structure(self):
        adds = [{"oracle_id": SOL_RING_OID, "name": "Sol Ring",
                 "qty": 2, "foil_qty": 0, "card_id": None}]
        body = _build_patch_body(adds, [], [], {})
        self.assertEqual(len(body["addCards"]), 1)
        a = body["addCards"][0]
        self.assertEqual(a["quantity"], 2)
        self.assertEqual(a["boardType"], "mainboard")
        self.assertEqual(a["finish"], "nonFoil")

    def test_foil_add_sets_finish_to_foil(self):
        adds = [{"oracle_id": SOL_RING_OID, "name": "Sol Ring",
                 "qty": 1, "foil_qty": 1, "card_id": None}]
        body = _build_patch_body(adds, [], [], {})
        self.assertEqual(body["addCards"][0]["finish"], "foil")

    def test_remove_without_card_id_skipped(self):
        removes = [{"oracle_id": BOLT_OID, "name": "Bolt",
                    "qty": 1, "card_id": None}]
        body = _build_patch_body([], removes, [], {})
        self.assertEqual(body["deleteCards"], [])

    def test_remove_with_card_id_included(self):
        removes = [{"oracle_id": BOLT_OID, "name": "Bolt",
                    "qty": 1, "card_id": "uuid-bolt"}]
        body = _build_patch_body([], removes, [], {})
        self.assertEqual(len(body["deleteCards"]), 1)
        self.assertEqual(body["deleteCards"][0]["cardId"], "uuid-bolt")

    def test_update_structure(self):
        updates = [{"oracle_id": SOL_RING_OID, "name": "Sol Ring",
                    "local_qty": 3, "remote_qty": 1, "foil_qty": 0,
                    "card_id": "uuid-sol"}]
        body = _build_patch_body([], [], updates, {})
        self.assertEqual(len(body["updateCards"]), 1)
        u = body["updateCards"][0]
        self.assertEqual(u["quantity"], 3)
        self.assertEqual(u["cardId"], "uuid-sol")


# ---------------------------------------------------------------------------
# 7. push_deck_diff (mocked HTTP)
# ---------------------------------------------------------------------------

class TestPushDeckDiff(unittest.TestCase):

    def setUp(self):
        _inject_type_index()
        import web_enrichment.moxfield_push as mp
        mp._bearer_cache = "fake-bearer-token"

    def tearDown(self):
        _clear_type_index()
        _clear_bearer_cache()

    @patch("web_enrichment.moxfield_push.httpx.request")
    def test_empty_diff_returns_sentinel_no_request(self, mock_req):
        result = push_deck_diff("deck-123", [], [], [])
        self.assertEqual(result["message"], "no_changes")
        mock_req.assert_not_called()

    @patch("web_enrichment.moxfield_push.httpx.request")
    def test_happy_path_sends_patch(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "deck-123", "name": "Test Deck"}
        mock_req.return_value = mock_resp

        adds = [{"oracle_id": SOL_RING_OID, "name": "Sol Ring",
                 "qty": 1, "foil_qty": 0, "card_id": None}]
        result = push_deck_diff("deck-123", adds, [], [])

        mock_req.assert_called_once()
        call_args = mock_req.call_args
        self.assertEqual(call_args[0][0], "PATCH")
        self.assertIn("deck-123", call_args[0][1])
        self.assertEqual(result["name"], "Test Deck")

    @patch("web_enrichment.moxfield_push.httpx.request")
    def test_401_triggers_refresh_and_retry(self, mock_req):
        """First request returns 401; after refresh, second returns 200."""
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.raise_for_status.side_effect = None  # not called in our path

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"id": "deck-123"}
        resp_200.raise_for_status = MagicMock()

        # First PATCH → 401; token refresh POST → 200 with new token;
        # second PATCH → 200
        refresh_resp = MagicMock()
        refresh_resp.status_code = 200
        refresh_resp.json.return_value = {
            "token": "new-bearer-token",
            "refreshToken": "new-refresh-uuid",
        }

        # _request_with_retry calls httpx.request for the main request;
        # _do_refresh calls httpx.post separately.
        with patch("web_enrichment.moxfield_push.httpx.post",
                   return_value=refresh_resp):
            # Sequence: PATCH → 401, then PATCH → 200
            mock_req.side_effect = [resp_401, resp_200]
            import web_enrichment.moxfield_push as mp
            mp._bearer_cache = "old-bearer"
            mp._refresh_cache = "old-refresh-uuid"
            with patch.dict(os.environ, {"MOXFIELD_REFRESH_TOKEN": "old-refresh-uuid"}):
                adds = [{"oracle_id": SOL_RING_OID, "name": "Sol Ring",
                         "qty": 1, "foil_qty": 0, "card_id": None}]
                result = push_deck_diff("deck-123", adds, [], [])
                self.assertEqual(result["id"], "deck-123")
                self.assertEqual(mock_req.call_count, 2)
                self.assertEqual(mp._bearer_cache, "new-bearer-token")


# ---------------------------------------------------------------------------
# 8. run_push (mocked HTTP + temp collection.db)
# ---------------------------------------------------------------------------

class TestRunPush(unittest.TestCase):

    def setUp(self):
        _inject_type_index()
        import web_enrichment.moxfield_push as mp
        mp._bearer_cache = "fake-bearer-token"

        self.tmpdir = tempfile.mkdtemp(prefix="mxrunpush_test_")
        self.db_path = os.path.join(self.tmpdir, "collection.db")
        self.conn = collection_db.get_connection(db_path=self.db_path)

        # Seed a small inventory
        self.conn.execute(
            """INSERT INTO inventory
               (name, set_code, collector_number, oracle_id, quantity,
                foil_quantity, first_scanned, last_scanned)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("Sol Ring", "lea", "1", SOL_RING_OID, 2, 0),
        )
        self.conn.commit()

    def tearDown(self):
        _clear_type_index()
        _clear_bearer_cache()
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _mock_remote_deck(self, cards: dict) -> MagicMock:
        """Build a mock response for get_moxfield_deck_cards."""
        deck_data = {
            "id": "deck-123",
            "name": "Test Deck",
            "mainboard": {},
        }
        for uuid, info in cards.items():
            deck_data["mainboard"][uuid] = {
                "quantity": info["qty"],
                "card": {
                    "oracle_id": info["oracle_id"],
                    "name": info.get("name", ""),
                    "set": info.get("set", ""),
                    "cn": info.get("cn", ""),
                },
            }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = deck_data
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    @patch("web_enrichment.moxfield_push.httpx.request")
    def test_dry_run_returns_diff_without_patching(self, mock_req):
        """dry_run=True should call GET but not PATCH."""
        # GET for the remote deck
        get_resp = self._mock_remote_deck({})  # empty remote deck
        mock_req.return_value = get_resp

        result = run_push("deck-123", self.conn, dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertFalse(result["committed"])
        # Sol Ring should be in adds
        add_oids = {a["oracle_id"] for a in result["adds"]}
        self.assertIn(SOL_RING_OID, add_oids)
        # Only one HTTP call (GET), not PATCH
        self.assertEqual(mock_req.call_count, 1)
        self.assertEqual(mock_req.call_args[0][0], "GET")

    @patch("web_enrichment.moxfield_push.httpx.request")
    def test_commit_sends_patch_and_updates_manifest(self, mock_req):
        """dry_run=False should GET remote deck, then PATCH, then update manifest."""
        # First call: GET deck (empty remote)
        get_resp = self._mock_remote_deck({})
        # Second call: PATCH
        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {"id": "deck-123", "name": "Test Deck"}
        patch_resp.raise_for_status = MagicMock()

        mock_req.side_effect = [get_resp, patch_resp]

        result = run_push("deck-123", self.conn, dry_run=False)

        self.assertFalse(result["dry_run"])
        self.assertTrue(result["committed"])
        self.assertIsNotNone(result["moxfield_response"])

        # Manifest should now have Sol Ring
        manifest = get_manifest(self.conn, "moxfield:deck-123")
        self.assertIn(SOL_RING_OID, manifest)
        self.assertEqual(manifest[SOL_RING_OID]["qty"], 2)

    @patch("web_enrichment.moxfield_push.httpx.request")
    def test_no_changes_does_not_patch(self, mock_req):
        """When local == remote, no PATCH should be sent."""
        # Remote deck already has Sol Ring qty=2
        get_resp = self._mock_remote_deck({
            "uuid-sol": {"oracle_id": SOL_RING_OID, "qty": 2, "name": "Sol Ring"}
        })
        mock_req.return_value = get_resp

        result = run_push("deck-123", self.conn, dry_run=False)

        self.assertEqual(result["total_changes"], 0)
        self.assertFalse(result["committed"])
        # Only GET, no PATCH
        self.assertEqual(mock_req.call_count, 1)

    @patch("web_enrichment.moxfield_push.httpx.request")
    def test_moxfield_error_raises_runtime_error(self, mock_req):
        """If Moxfield GET returns 404, run_push raises RuntimeError."""
        import httpx as _httpx
        err_resp = MagicMock()
        err_resp.status_code = 404
        err_resp.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "404 Not Found", request=MagicMock(), response=err_resp
        )
        mock_req.return_value = err_resp

        with self.assertRaises(RuntimeError) as ctx:
            run_push("bad-deck-id", self.conn, dry_run=True)
        self.assertIn("404", str(ctx.exception))


# ---------------------------------------------------------------------------
# 9. get_bearer auth tests
# ---------------------------------------------------------------------------

class TestGetBearer(unittest.TestCase):

    def setUp(self):
        _clear_bearer_cache()

    def tearDown(self):
        _clear_bearer_cache()

    def test_returns_env_token_on_first_call(self):
        with patch.dict(os.environ, {"MOXFIELD_BEARER_TOKEN": "env-bearer-abc"}):
            from web_enrichment.moxfield_push import get_bearer
            import web_enrichment.moxfield_push as mp
            mp._bearer_cache = None
            token = get_bearer()
            self.assertEqual(token, "env-bearer-abc")

    def test_caches_bearer_in_memory(self):
        with patch.dict(os.environ, {"MOXFIELD_BEARER_TOKEN": "env-bearer-abc"}):
            import web_enrichment.moxfield_push as mp
            mp._bearer_cache = None
            from web_enrichment.moxfield_push import get_bearer
            _ = get_bearer()
            # Second call should return cache, not re-read env
            self.assertEqual(mp._bearer_cache, "env-bearer-abc")

    def test_missing_bearer_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "MOXFIELD_BEARER_TOKEN"}
        env.pop("MOXFIELD_BEARER_TOKEN", None)
        import web_enrichment.moxfield_push as mp
        mp._bearer_cache = None
        with patch.dict(os.environ, env, clear=True):
            from web_enrichment.moxfield_push import get_bearer, _env_bearer
            with self.assertRaises(RuntimeError) as ctx:
                _env_bearer()
            self.assertIn("MOXFIELD_BEARER_TOKEN", str(ctx.exception))

    @patch("web_enrichment.moxfield_push.httpx.post")
    def test_force_refresh_calls_refresh_endpoint(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"token": "refreshed-bearer"}),
        )
        with patch.dict(os.environ, {"MOXFIELD_REFRESH_TOKEN": "test-refresh"}):
            import web_enrichment.moxfield_push as mp
            mp._bearer_cache = "old-bearer"
            mp._refresh_cache = "test-refresh"
            from web_enrichment.moxfield_push import get_bearer
            new_token = get_bearer(force_refresh=True)
            self.assertEqual(new_token, "refreshed-bearer")
            mock_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
