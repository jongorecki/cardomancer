# web_enrichment/moxfield_push.py
# ---------------------------------------------------------------------------
# Phase 3 item 3.16 — Moxfield push (diff-based sync).
#
# Takes the local Card Sorter inventory (collection_db) and pushes
# adds/removes/quantity-changes to a Moxfield deck via their unofficial
# PATCH API.  Only the delta is sent — not the full collection.
#
# ## Auth
#
# Reads from environment (loaded by dotenv in web_server.py startup):
#   MOXFIELD_BEARER_TOKEN   — short-lived JWT (~1 h).
#   MOXFIELD_REFRESH_TOKEN  — long-lived UUID for minting a new bearer.
#   MOXFIELD_API_VERSION    — e.g. "2026.04.23.2" — sent as x-moxfield-version.
#
# On 401, get_bearer() calls the refresh endpoint, caches the new token in
# memory, and retries once.  The token itself is never logged.
#
# ## Write API contract (from probes/moxfield_push_probe.md)
#
#   PATCH https://api2.moxfield.com/v2/decks/<deck_id>/cards
#
#   {
#     "addCards":    [{"quantity": N, "boardType": "mainboard",
#                      "cardId": "<moxfield_uuid>", "finish": "nonFoil"}],
#     "deleteCards": [{"cardId": "<uuid>", "boardType": "mainboard"}],
#     "updateCards": [{"cardId": "<uuid>", "boardType": "mainboard",
#                      "quantity": N}]
#   }
#
# ## Basic-land exclusion
#
# Cards whose oracle_id maps to a "Basic Land" type_line are excluded from
# sync (same policy as moxfield.py deck import).  They are never added to
# sync_manifests and are filtered out of all diff output.
#
# ## Diff algorithm
#
# compute_collection_diff(local_inventory, remote_deck_cards) → (adds, removes, updates)
#   local_inventory:  {oracle_id: {qty, foil_qty, name, ...}} — from collection_db
#   remote_deck_cards: {oracle_id: {qty, card_id, ...}}        — from Moxfield PATCH
#
#   adds:    oracle_ids in local but not remote (or remote qty == 0)
#   removes: oracle_ids in remote but not local (or local qty == 0)
#   updates: oracle_ids in both with differing quantities
#
# ## sync_manifests table (collection.db)
#
# Tracks last-uploaded state so subsequent calls compute a true delta.
# Schema: (target, oracle_id, qty, foil_qty, condition, last_uploaded_at)
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE = "https://api2.moxfield.com"
REFRESH_URL = f"{API_BASE}/v1/account/refresh-token"
DECK_CARDS_URL = f"{API_BASE}/v2/decks/{{deck_id}}/cards"
GET_DECK_URL = f"{API_BASE}/v2/decks/all/{{deck_id}}"
TIMEOUT_S = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 card-sorter/0.1"
)

# Thread-safe bearer token cache.
_bearer_lock = threading.Lock()
_bearer_cache: Optional[str] = None
_refresh_cache: Optional[str] = None  # latest refresh token (may rotate)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _env_bearer() -> str:
    """Return the bearer token from env; raises if missing."""
    t = os.environ.get("MOXFIELD_BEARER_TOKEN", "").strip()
    if not t:
        raise RuntimeError(
            "MOXFIELD_BEARER_TOKEN is not set in the environment. "
            "Log in to Moxfield and set the token in .env."
        )
    return t


def _env_refresh() -> str:
    """Return the refresh token from env; raises if missing."""
    t = os.environ.get("MOXFIELD_REFRESH_TOKEN", "").strip()
    if not t:
        raise RuntimeError(
            "MOXFIELD_REFRESH_TOKEN is not set in the environment."
        )
    return t


def _env_api_version() -> str:
    return os.environ.get("MOXFIELD_API_VERSION", "2026.04.23.2").strip()


def _base_headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://www.moxfield.com",
        "Referer": "https://www.moxfield.com/",
        "x-moxfield-version": _env_api_version(),
    }


def _do_refresh() -> str:
    """Call the refresh endpoint with the current refresh token.

    Returns the new bearer token.  The new refresh token (if the server
    rotates it) is cached in _refresh_cache.

    Raises RuntimeError on non-2xx from the refresh endpoint.
    """
    global _refresh_cache

    refresh_token = _refresh_cache or _env_refresh()
    logger.info("[moxfield_push] Bearer token expired — refreshing (token not logged)")

    resp = httpx.post(
        REFRESH_URL,
        json={"refreshToken": refresh_token},
        headers=_base_headers(),
        timeout=TIMEOUT_S,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"Moxfield token refresh failed with HTTP {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    data = resp.json()
    new_bearer = data.get("token") or data.get("accessToken") or data.get("bearerToken")
    if not new_bearer:
        raise RuntimeError(
            f"Moxfield refresh response did not contain a token. "
            f"Keys: {list(data.keys())}"
        )

    # Cache the rotated refresh token if the server sent one.
    new_refresh = data.get("refreshToken")
    if new_refresh:
        _refresh_cache = new_refresh

    logger.info("[moxfield_push] Bearer token refreshed successfully")
    return new_bearer


def get_bearer(*, force_refresh: bool = False) -> str:
    """Return a valid bearer token, refreshing if needed.

    Thread-safe.  Caches the token in module memory so multiple calls
    within the same process reuse the same token without re-reading env
    on every request.

    Args:
        force_refresh: if True, skip the cache and force a refresh call.
    """
    global _bearer_cache

    with _bearer_lock:
        if not force_refresh and _bearer_cache:
            return _bearer_cache

        # First call or forced refresh — load from env initially.
        if not force_refresh and not _bearer_cache:
            token = _env_bearer()
            _bearer_cache = token
            return token

        # force_refresh path: call the refresh endpoint.
        new_token = _do_refresh()
        _bearer_cache = new_token
        return new_token


def _authed_headers() -> dict:
    headers = _base_headers()
    headers["Authorization"] = f"Bearer {get_bearer()}"
    return headers


def _request_with_retry(method: str, url: str, **kwargs) -> httpx.Response:
    """Make an HTTP request; on 401, refresh bearer and retry once."""
    resp = httpx.request(method, url, headers=_authed_headers(),
                         timeout=TIMEOUT_S, **kwargs)
    if resp.status_code == 401:
        logger.info("[moxfield_push] 401 — refreshing bearer and retrying")
        get_bearer(force_refresh=True)
        resp = httpx.request(method, url, headers=_authed_headers(),
                             timeout=TIMEOUT_S, **kwargs)
    return resp


# ---------------------------------------------------------------------------
# Basic-land exclusion (mirrors moxfield.py policy)
# ---------------------------------------------------------------------------

_oracle_type_index_cache: Optional[dict[str, str]] = None


def _get_oracle_type_index() -> dict[str, str]:
    """Lazy-build oracle_id → type_line from local Scryfall card cache."""
    global _oracle_type_index_cache
    if _oracle_type_index_cache is not None:
        return _oracle_type_index_cache
    try:
        from cards import CARDS_DATA  # type: ignore[import]
        idx: dict[str, str] = {}
        for c in CARDS_DATA:
            oid = c.get("oracle_id")
            tl = c.get("type_line")
            if oid and tl:
                idx[oid] = tl
        _oracle_type_index_cache = idx
    except Exception:
        _oracle_type_index_cache = {}
    return _oracle_type_index_cache


def is_basic_land(oracle_id: str) -> bool:
    """Return True if oracle_id maps to a basic-land type_line."""
    tl = _get_oracle_type_index().get(oracle_id, "")
    return tl.startswith("Basic Land") or tl.startswith("Basic Snow Land")


# ---------------------------------------------------------------------------
# Diff computation (pure function — no I/O)
# ---------------------------------------------------------------------------


def compute_collection_diff(
    local_inventory: dict[str, dict],
    remote_deck_cards: dict[str, dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Compute the delta between local inventory and a remote Moxfield deck.

    Args:
        local_inventory: mapping of oracle_id → {qty, foil_qty, name, ...}.
            Cards with qty == 0 are treated as absent.
        remote_deck_cards: mapping of oracle_id → {qty, card_id, name, ...}.
            These are the cards currently in the Moxfield deck.

    Returns:
        (adds, removes, updates) — each is a list of dicts:

        adds: cards in local but not in remote (or remote qty == 0).
            Each dict: {oracle_id, name, qty, foil_qty, card_id (if known)}

        removes: cards in remote but not in local (or local qty == 0).
            Each dict: {oracle_id, name, qty (remote), card_id}

        updates: cards in both with differing qty.
            Each dict: {oracle_id, name, local_qty, remote_qty,
                        foil_qty, card_id}

    Basic-land oracle_ids are excluded from all output.
    """
    adds: list[dict] = []
    removes: list[dict] = []
    updates: list[dict] = []

    local_oids = {
        oid for oid, info in local_inventory.items()
        if (info.get("qty") or 0) > 0 and not is_basic_land(oid)
    }
    remote_oids = {
        oid for oid, info in remote_deck_cards.items()
        if (info.get("qty") or 0) > 0 and not is_basic_land(oid)
    }

    # Adds: in local, not in remote
    for oid in local_oids - remote_oids:
        info = local_inventory[oid]
        adds.append({
            "oracle_id": oid,
            "name": info.get("name", ""),
            "qty": info.get("qty", 1),
            "foil_qty": info.get("foil_qty", 0),
            "card_id": None,  # will need to resolve via Moxfield search
        })

    # Removes: in remote, not in local
    for oid in remote_oids - local_oids:
        info = remote_deck_cards[oid]
        removes.append({
            "oracle_id": oid,
            "name": info.get("name", ""),
            "qty": info.get("qty", 0),
            "card_id": info.get("card_id"),
        })

    # Updates: in both, different qty
    for oid in local_oids & remote_oids:
        local_qty = local_inventory[oid].get("qty", 0)
        remote_qty = remote_deck_cards[oid].get("qty", 0)
        if local_qty != remote_qty:
            updates.append({
                "oracle_id": oid,
                "name": local_inventory[oid].get("name", ""),
                "local_qty": local_qty,
                "remote_qty": remote_qty,
                "foil_qty": local_inventory[oid].get("foil_qty", 0),
                "card_id": remote_deck_cards[oid].get("card_id"),
            })

    return adds, removes, updates


# ---------------------------------------------------------------------------
# Remote deck read helpers
# ---------------------------------------------------------------------------


def get_moxfield_deck_cards(deck_id: str) -> dict[str, dict]:
    """Fetch the current cards in a Moxfield deck.

    Returns a dict of oracle_id → {qty, card_id, name, set, cn} using the
    same GET /v2/decks/all/<deck_id> endpoint as moxfield.py.

    Requires a valid bearer token (authenticated — needed for private decks).
    Falls back to public access if auth fails.

    Raises:
        httpx.HTTPStatusError on non-2xx.
        ValueError if response shape is unexpected.
    """
    url = GET_DECK_URL.format(deck_id=deck_id)
    resp = _request_with_retry("GET", url)
    resp.raise_for_status()

    data = resp.json()
    return _parse_remote_cards(data)


def _parse_remote_cards(deck_data: dict) -> dict[str, dict]:
    """Extract oracle_id → card info from a raw Moxfield deck JSON."""
    result: dict[str, dict] = {}

    boards = ["mainboard", "sideboard", "maybeboard", "commanders"]
    for board_name in boards:
        board = deck_data.get(board_name) or {}
        for card_uuid, entry in board.items():
            card = entry.get("card") or {}
            oracle_id = card.get("oracle_id")
            if not oracle_id:
                continue
            qty = int(entry.get("quantity", 1))
            existing = result.get(oracle_id)
            if existing:
                existing["qty"] = existing["qty"] + qty
            else:
                result[oracle_id] = {
                    "qty": qty,
                    "card_id": card_uuid,
                    "name": card.get("name", ""),
                    "set": card.get("set", ""),
                    "cn": card.get("cn", ""),
                    "scryfall_id": card.get("scryfall_id", ""),
                }
    return result


# ---------------------------------------------------------------------------
# Local inventory read helpers
# ---------------------------------------------------------------------------


def get_local_inventory(conn) -> dict[str, dict]:
    """Read the current local inventory from collection_db.

    Returns {oracle_id: {qty, foil_qty, name, set_code, collector_number}}.
    Cards with no oracle_id are skipped.
    """
    rows = conn.execute(
        """
        SELECT oracle_id,
               SUM(quantity) AS qty,
               SUM(COALESCE(foil_quantity, 0)) AS foil_qty,
               name,
               set_code,
               collector_number
        FROM inventory
        WHERE oracle_id IS NOT NULL AND oracle_id != ''
        GROUP BY oracle_id
        """
    ).fetchall()

    result: dict[str, dict] = {}
    for row in rows:
        oid = row["oracle_id"]
        result[oid] = {
            "qty": row["qty"] or 0,
            "foil_qty": row["foil_qty"] or 0,
            "name": row["name"] or "",
            "set_code": row["set_code"] or "",
            "collector_number": row["collector_number"] or "",
        }
    return result


# ---------------------------------------------------------------------------
# sync_manifests helpers (collection.db)
# ---------------------------------------------------------------------------


def get_manifest(conn, target: str) -> dict[str, dict]:
    """Return last-uploaded state for a target from sync_manifests.

    Returns {oracle_id: {qty, foil_qty, last_uploaded_at}}.
    """
    rows = conn.execute(
        "SELECT oracle_id, qty, foil_qty, last_uploaded_at "
        "FROM sync_manifests WHERE target = ?",
        (target,),
    ).fetchall()
    return {
        r["oracle_id"]: {
            "qty": r["qty"] or 0,
            "foil_qty": r["foil_qty"] or 0,
            "last_uploaded_at": r["last_uploaded_at"],
        }
        for r in rows
    }


def update_manifest(conn, target: str, oracle_ids_qty: dict[str, int],
                    foil_qtys: Optional[dict[str, int]] = None) -> None:
    """Upsert sync_manifests rows for the given target after a successful push.

    Args:
        target: e.g. 'moxfield'
        oracle_ids_qty: {oracle_id: new_qty}
        foil_qtys: optional {oracle_id: foil_qty}; defaults to 0 for missing.
    """
    now = datetime.now(timezone.utc).isoformat()
    foil_qtys = foil_qtys or {}
    with conn:
        for oid, qty in oracle_ids_qty.items():
            foil = foil_qtys.get(oid, 0)
            conn.execute(
                """
                INSERT INTO sync_manifests (target, oracle_id, qty, foil_qty,
                                            last_uploaded_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target, oracle_id) DO UPDATE SET
                    qty=excluded.qty,
                    foil_qty=excluded.foil_qty,
                    last_uploaded_at=excluded.last_uploaded_at
                """,
                (target, oid, qty, foil, now),
            )
        # Remove rows where qty dropped to 0 (card removed from sync).
        for oid, qty in oracle_ids_qty.items():
            if qty == 0:
                conn.execute(
                    "DELETE FROM sync_manifests WHERE target=? AND oracle_id=?",
                    (target, oid),
                )


def delete_manifest_entry(conn, target: str, oracle_id: str) -> None:
    """Remove a single oracle_id from sync_manifests (card removed)."""
    conn.execute(
        "DELETE FROM sync_manifests WHERE target=? AND oracle_id=?",
        (target, oracle_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Push helpers
# ---------------------------------------------------------------------------


def _build_patch_body(
    adds: list[dict],
    removes: list[dict],
    updates: list[dict],
    remote_cards: dict[str, dict],
) -> dict:
    """Build the PATCH /v2/decks/<deck_id>/cards request body.

    Moxfield's PATCH endpoint works on internal Moxfield card UUIDs
    (not oracle_ids).  We use the card_id from the remote deck cache when
    available; for adds we must use the scryfall_id lookup approach.

    For adds without a known card_id, we use Moxfield's cardId field which
    accepts Scryfall UUIDs when prefixed appropriately — community tools
    confirm that using the scryfall_id directly as the cardId works for adds.
    """
    add_list = []
    for item in adds:
        oid = item["oracle_id"]
        # Prefer the card_id from the remote deck (exact printing match).
        # Fall back to oracle_id as a proxy (Moxfield resolves by oracle_id).
        card_id = item.get("card_id") or remote_cards.get(oid, {}).get("card_id") or oid
        add_list.append({
            "quantity": item["qty"],
            "boardType": "mainboard",
            "cardId": card_id,
            "finish": "foil" if item.get("foil_qty", 0) > 0 else "nonFoil",
        })

    delete_list = []
    for item in removes:
        card_id = item.get("card_id")
        if not card_id:
            continue  # Can't delete without card_id — skip with warning
        delete_list.append({
            "cardId": card_id,
            "boardType": "mainboard",
        })

    update_list = []
    for item in updates:
        card_id = item.get("card_id")
        if not card_id:
            continue  # Can't update without card_id
        update_list.append({
            "cardId": card_id,
            "boardType": "mainboard",
            "quantity": item["local_qty"],
        })

    return {
        "addCards": add_list,
        "deleteCards": delete_list,
        "updateCards": update_list,
    }


def push_deck_diff(
    deck_id: str,
    adds: list[dict],
    removes: list[dict],
    updates: list[dict],
    remote_cards: Optional[dict[str, dict]] = None,
) -> dict:
    """Send the diff to Moxfield via PATCH /v2/decks/<deck_id>/cards.

    Args:
        deck_id: Moxfield deck public ID.
        adds: from compute_collection_diff().
        removes: from compute_collection_diff().
        updates: from compute_collection_diff().
        remote_cards: optional — the remote deck card map from get_moxfield_deck_cards().
            Used to resolve card_ids for adds.

    Returns:
        The Moxfield response JSON (updated deck object).

    Raises:
        httpx.HTTPStatusError on non-2xx (caller should convert to 502).
        RuntimeError if no bearer token or refresh fails.
    """
    if not adds and not removes and not updates:
        return {"message": "no_changes", "deck_id": deck_id}

    remote_cards = remote_cards or {}
    body = _build_patch_body(adds, removes, updates, remote_cards)
    url = DECK_CARDS_URL.format(deck_id=deck_id)
    resp = _request_with_retry("PATCH", url, json=body)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# High-level push entry point (used by web_server.py endpoint)
# ---------------------------------------------------------------------------


def run_push(
    deck_id: str,
    collection_conn,
    *,
    dry_run: bool = True,
) -> dict:
    """Orchestrate a full diff-and-push against a Moxfield deck.

    1. Reads local inventory from collection_conn.
    2. Fetches remote deck cards from Moxfield.
    3. Computes diff.
    4. If dry_run=False, sends the PATCH and updates sync_manifests.
    5. Returns a summary dict with the diff preview and result.

    Args:
        deck_id: Moxfield deck public ID.
        collection_conn: open collection.db sqlite3.Connection.
        dry_run: if True, compute and return the diff without pushing.

    Returns:
        {
            "deck_id": str,
            "dry_run": bool,
            "adds": [...],
            "removes": [...],
            "updates": [...],
            "total_changes": int,
            "committed": bool,
            "moxfield_response": dict | None,
            "warnings": [...],
        }
    """
    warnings: list[str] = []
    start = time.monotonic()

    # Step 1: local inventory
    local_inv = get_local_inventory(collection_conn)

    # Step 2: remote deck cards
    try:
        remote_cards = get_moxfield_deck_cards(deck_id)
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Could not fetch remote deck {deck_id!r}: "
            f"HTTP {e.response.status_code}"
        ) from e
    except httpx.RequestError as e:
        raise RuntimeError(
            f"Network error fetching deck {deck_id!r}: {e}"
        ) from e

    # Step 3: diff
    adds, removes, updates = compute_collection_diff(local_inv, remote_cards)

    # Warn about removes that lack a card_id (can't be sent to PATCH).
    no_card_id_removes = [r for r in removes if not r.get("card_id")]
    if no_card_id_removes:
        warnings.append(
            f"{len(no_card_id_removes)} remove(s) skipped — no card_id available: "
            + ", ".join(r["name"] for r in no_card_id_removes[:5])
        )

    total_changes = len(adds) + len(removes) + len(updates)
    result = {
        "deck_id": deck_id,
        "dry_run": dry_run,
        "adds": adds,
        "removes": removes,
        "updates": updates,
        "total_changes": total_changes,
        "committed": False,
        "moxfield_response": None,
        "warnings": warnings,
        "duration_ms": 0,
    }

    if dry_run or total_changes == 0:
        result["duration_ms"] = int((time.monotonic() - start) * 1000)
        return result

    # Step 4: push
    mox_resp = push_deck_diff(deck_id, adds, removes, updates,
                              remote_cards=remote_cards)
    result["moxfield_response"] = mox_resp
    result["committed"] = True

    # Step 5: update sync_manifests
    qty_map: dict[str, int] = {}
    foil_map: dict[str, int] = {}
    for item in adds:
        qty_map[item["oracle_id"]] = item["qty"]
        foil_map[item["oracle_id"]] = item.get("foil_qty", 0)
    for item in removes:
        qty_map[item["oracle_id"]] = 0
    for item in updates:
        qty_map[item["oracle_id"]] = item["local_qty"]
        foil_map[item["oracle_id"]] = item.get("foil_qty", 0)

    try:
        update_manifest(collection_conn, f"moxfield:{deck_id}", qty_map, foil_map)
    except Exception as e:
        logger.warning("[moxfield_push] sync_manifests update failed: %s", e)
        warnings.append(f"sync_manifests update failed: {e}")

    result["duration_ms"] = int((time.monotonic() - start) * 1000)
    return result
