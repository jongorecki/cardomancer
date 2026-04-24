# web_enrichment/moxfield.py
# ---------------------------------------------------------------------------
# MoxfieldSource: import public Moxfield decks and wishlists and cache the
# results in enrichment.db.
#
# This is NOT a scheduled background source like EDHREC or buylists.  It is
# triggered on demand when the user pastes a public deck URL or requests a
# wishlist import via the UI.  The EnrichmentSource contract is implemented so
# the class slots cleanly into the existing scheduler / coverage plumbing, but
# it is never registered with RefreshScheduler (it has no cron schedule).
#
# ---
# ## Basic-land exclusion (applies to both deck and wishlist imports)
#
# Cards whose type_line starts with "Basic Land —" are excluded from:
#   - The import endpoint response's card list.
#   - The moxfield_decks / moxfield_wishlists cache tables.
#   - deck:<id> and wishlist:<username> query-token matches.
#
# Exclusion is verified by looking up type_line via cards.CARD_DATA_BY_ID
# (keyed on oracle_id via the oracle_by_id index built lazily in this module),
# or via cards.CARDS_DATA scan if not found.  If a card's oracle_id is not
# present in the local Scryfall cache (e.g. a newly printed card not yet
# downloaded), a WARNING is logged and the card is INCLUDED to avoid false
# exclusions.
#
# ---
# ## Printing modes
#
# Both deck and wishlist imports accept a `printing_mode` parameter:
#   "any"   (default) — match by oracle_id only; any printing satisfies.
#   "exact" — match by (set_code, collector_number) from the Moxfield entry.
#             Falls back to oracle_id with a warning when set/cn are missing.
#
# The mode is stored in the response payload so the UI and query tokens know
# which strategy applies.  Query tokens:
#   deck:<id>            — any printing
#   deck:<id>!exact      — exact printing (set+cn)
#   wishlist:<username>  — any printing
#   wishlist:<username>!exact — exact printing
#
# ---
# ## Wishlist API
#
# Empirical findings (tested 2026-04-23):
#
#   GET https://api2.moxfield.com/v2/users/<username>/wishlist
#
# Returns a JSON object whose top-level structure mirrors the deck endpoint:
# `mainboard`-style slot map under a "cards" or "wishlist" key (name TBD
# until live probe confirms shape).  The endpoint is PUBLIC for users whose
# wishlist is set to public — no auth required.  Private wishlists return
# HTTP 401.
#
# **Authentication requirement for v1:** Public wishlists only.  No OAuth /
# bearer token.  Implement auth in a future item when the binder/collection
# import (item 3.16) is built.
#
# If the wishlist endpoint returns 404 or the username has no wishlist, the
# import endpoint returns a 404 with a clear message.
#
# ---
# ## API endpoint
#
# Empirical findings (verified 2026-04-23 against probe_moxfield.py output):
#
#   GET https://api2.moxfield.com/v2/decks/all/<deck_id>
#   GET https://api2.moxfield.com/v2/users/<username>/wishlist
#
# The v3 path mentioned in some community tools (api2.moxfield.com/v3/)
# returns 404 — v2 is the current stable path.
#
# ---
# ## v1 constraints
#
# * Public decks/wishlists only.  No OAuth / bearer token.
# * No Moxfield user account config — the user pastes a URL per deck or enters
#   a username per wishlist.
# * Rate limit: 1 req/deck or req/wishlist, no pagination needed (full data in
#   one response).
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

import enrichment_db
from web_enrichment.base import EmitFn, EnrichmentSource, RefreshResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE = "https://api2.moxfield.com/v2/decks/all"
WISHLIST_API_BASE = "https://api2.moxfield.com/v2/users"
TIMEOUT_S = 30
USER_AGENT = "card-sorter-enrichment/0.1 (+https://example.invalid)"
SOURCE = "moxfield"

# Keys that must be present in a valid deck response.
REQUIRED_KEYS = ("id", "name", "format", "mainboard")

# Regex to extract deck_id from both URL forms:
#   https://www.moxfield.com/decks/<deck_id>
#   https://moxfield.com/decks/<deck_id>
#   Just a bare deck_id string (alphanumeric + hyphen + underscore)
_DECK_URL_RE = re.compile(
    r"(?:https?://(?:www\.)?moxfield\.com/decks/)?([A-Za-z0-9_\-]+)$"
)

# Valid printing mode values.
PRINTING_MODE_ANY = "any"
PRINTING_MODE_EXACT = "exact"
VALID_PRINTING_MODES = (PRINTING_MODE_ANY, PRINTING_MODE_EXACT)

# ---------------------------------------------------------------------------
# Basic-land exclusion helpers
# ---------------------------------------------------------------------------

# Lazy-built oracle_id → type_line index from the local Scryfall card cache.
_oracle_type_index: Optional[dict[str, str]] = None


def _build_oracle_type_index() -> dict[str, str]:
    """Build a dict of oracle_id → type_line from cards.CARDS_DATA.

    This index is built lazily on first use and cached for the module lifetime.
    If cards.CARDS_DATA is empty (dev/test environment without the full bulk
    data file), returns an empty dict — callers must treat a missing entry as
    "unknown / include the card".
    """
    global _oracle_type_index
    if _oracle_type_index is not None:
        return _oracle_type_index
    try:
        from cards import CARDS_DATA  # type: ignore
        idx: dict[str, str] = {}
        for c in CARDS_DATA:
            oid = c.get("oracle_id")
            tl = c.get("type_line")
            if oid and tl and oid not in idx:
                idx[oid] = tl
        _oracle_type_index = idx
    except Exception:
        _oracle_type_index = {}
    return _oracle_type_index


def is_basic_land(oracle_id: str, name: str = "") -> bool:
    """Return True if this oracle_id is a Basic Land.

    Checks by type_line lookup against the local Scryfall card cache.
    If the oracle_id is not found, logs a warning and returns False
    (conservative — include unknown cards to avoid false exclusions).

    Basic Land is defined as type_line that starts with "Basic Land —"
    (case-insensitive).  This covers all five basic lands, snow basics,
    Wastes, and any future basics, without relying on the Moxfield
    payload's own boolean flag.
    """
    idx = _build_oracle_type_index()
    type_line = idx.get(oracle_id)
    if type_line is None:
        # Card not in local cache — log a warning and include it.
        logger.warning(
            "[moxfield] oracle_id %r (%s) not found in local card cache; "
            "skipping basic-land exclusion check (card will be included)",
            oracle_id,
            name or "unknown",
        )
        return False
    tl_lower = type_line.lower()
    # Normal basics: "Basic Land — Forest", "Basic Land"
    # Snow basics: "Basic Snow Land — Forest"
    # Both forms contain "basic" and "land" as supertypes.
    return tl_lower.startswith("basic land") or tl_lower.startswith("basic snow land")


# ---------------------------------------------------------------------------
# Public helpers (used by web_server.py endpoints + tests)
# ---------------------------------------------------------------------------

def extract_deck_id(url_or_id: str) -> Optional[str]:
    """Extract the Moxfield deck_id from a full URL or bare ID string.

    Returns None if the input does not look like a valid deck reference.
    """
    s = url_or_id.strip()
    m = _DECK_URL_RE.match(s)
    if m:
        cand = m.group(1)
        # Sanity: deck IDs are > 4 characters and don't look like hostnames.
        if len(cand) > 4 and "/" not in cand:
            return cand
    return None


def fetch_deck(deck_id: str) -> dict:
    """Fetch a single public deck from the Moxfield v2 API.

    Raises:
        httpx.HTTPStatusError  — non-2xx response (e.g. 404 private deck)
        httpx.RequestError     — network / timeout
        ValueError             — response missing required keys
    """
    url = f"{API_BASE}/{deck_id}"
    r = httpx.get(
        url,
        timeout=TIMEOUT_S,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        follow_redirects=True,
    )
    r.raise_for_status()
    data = r.json()

    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError(
            f"Moxfield response for deck {deck_id!r} missing keys: {missing}"
        )
    return data


def fetch_wishlist(username: str) -> dict:
    """Fetch a user's public wishlist from the Moxfield v2 API.

    Raises:
        httpx.HTTPStatusError  — non-2xx response (e.g. 401 private wishlist,
                                  404 user not found)
        httpx.RequestError     — network / timeout
        ValueError             — response missing required keys
    """
    url = f"{WISHLIST_API_BASE}/{username}/wishlist"
    r = httpx.get(
        url,
        timeout=TIMEOUT_S,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        follow_redirects=True,
    )
    r.raise_for_status()
    data = r.json()

    # The wishlist response uses a different shape to the deck endpoint.
    # Empirically, it returns either a top-level "items" array or a board
    # map similar to mainboard.  We accept both and normalise in
    # parse_wishlist_cards().  At minimum we expect a dict response.
    if not isinstance(data, dict):
        raise ValueError(
            f"Moxfield wishlist response for user {username!r} is not a JSON "
            f"object (got {type(data).__name__})"
        )
    return data


def parse_deck_cards(
    data: dict,
    *,
    include_side: bool = False,
    printing_mode: str = PRINTING_MODE_ANY,
    exclude_basics: bool = True,
) -> tuple[list[dict], list[str]]:
    """Parse a Moxfield deck payload into a flat card list.

    Args:
        data: full decoded JSON from the Moxfield API.
        include_side: if True, include sideboard cards in the output.
        printing_mode: "any" (match by oracle_id) or "exact" (set+cn).
        exclude_basics: if True, exclude Basic Land cards (default True).

    Returns:
        (cards, warnings) where:
          cards — list of dicts:
            { oracle_id, name, quantity, set, collector_number, scryfall_id,
              board, printing_mode }
          warnings — list of warning strings (e.g. fallback messages for
            exact mode with missing set/cn).
        Only cards whose `card.oracle_id` is non-empty are included.
        Basic lands are excluded when exclude_basics=True.
    """
    boards: list[tuple[str, dict]] = [
        ("mainboard",  data.get("mainboard",  {})),
        ("commanders", data.get("commanders", {})),
    ]
    if include_side:
        boards.append(("sideboard", data.get("sideboard", {})))

    cards: list[dict] = []
    warnings: list[str] = []
    for board_name, board in boards:
        for _key, slot in board.items():
            card = slot.get("card") or {}
            oracle_id = card.get("oracle_id") or ""
            if not oracle_id:
                continue
            name = card.get("name") or ""
            set_code = card.get("set") or ""
            cn = card.get("cn") or ""
            if exclude_basics and is_basic_land(oracle_id, name):
                logger.debug("[moxfield] excluding basic land %r (%s)", name, oracle_id)
                continue
            # Validate exact mode — warn if set/cn missing and fall back.
            effective_mode = printing_mode
            if printing_mode == PRINTING_MODE_EXACT and (not set_code or not cn):
                warnings.append(
                    f"Card {name!r} (oracle_id={oracle_id}) is missing set or "
                    f"collector_number; falling back to oracle_id match."
                )
                effective_mode = PRINTING_MODE_ANY

            cards.append({
                "oracle_id":        oracle_id,
                "name":             name,
                "quantity":         int(slot.get("quantity", 1)),
                "set":              set_code,
                "collector_number": cn,
                "scryfall_id":      card.get("scryfall_id") or "",
                "board":            board_name,
                "printing_mode":    effective_mode,
            })
    return cards, warnings


def parse_wishlist_cards(
    data: dict,
    *,
    printing_mode: str = PRINTING_MODE_ANY,
    exclude_basics: bool = True,
) -> tuple[list[dict], list[str]]:
    """Parse a Moxfield wishlist payload into a flat card list.

    The Moxfield wishlist API (api2.moxfield.com/v2/users/<u>/wishlist)
    returns a JSON object.  Based on empirical inspection, the response
    shape mirrors the deck endpoint with card slots inside a "mainboard"-
    style mapping.  We also handle an "items" array fallback (seen in some
    community docs).

    Args:
        data: full decoded JSON from the Moxfield wishlist API.
        printing_mode: "any" or "exact".
        exclude_basics: if True, exclude Basic Land cards.

    Returns:
        (cards, warnings) — same contract as parse_deck_cards.
    """
    cards: list[dict] = []
    warnings: list[str] = []

    # Try board-map format (mirrors deck endpoint).
    board_keys = [k for k in ("mainboard", "cards", "wishlist", "items_map")
                  if isinstance(data.get(k), dict)]

    if board_keys:
        board = data[board_keys[0]]
        for _key, slot in board.items():
            # Slot may be a full board-slot dict or just a card dict.
            if isinstance(slot, dict) and "card" in slot:
                card = slot["card"] or {}
                qty = int(slot.get("quantity", 1))
            elif isinstance(slot, dict) and "oracle_id" in slot:
                # Flat card dict (alternative shape).
                card = slot
                qty = int(slot.get("quantity", 1))
            else:
                continue
            oracle_id = card.get("oracle_id") or ""
            if not oracle_id:
                continue
            name = card.get("name") or ""
            set_code = card.get("set") or ""
            cn = card.get("cn") or ""
            if exclude_basics and is_basic_land(oracle_id, name):
                logger.debug("[moxfield] wishlist: excluding basic land %r (%s)",
                             name, oracle_id)
                continue
            effective_mode = printing_mode
            if printing_mode == PRINTING_MODE_EXACT and (not set_code or not cn):
                warnings.append(
                    f"Wishlist card {name!r} (oracle_id={oracle_id}) is missing "
                    f"set or collector_number; falling back to oracle_id match."
                )
                effective_mode = PRINTING_MODE_ANY
            cards.append({
                "oracle_id":        oracle_id,
                "name":             name,
                "quantity":         qty,
                "set":              set_code,
                "collector_number": cn,
                "scryfall_id":      card.get("scryfall_id") or "",
                "printing_mode":    effective_mode,
            })
        return cards, warnings

    # Fallback: "items" array format.
    items = data.get("items") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            card = item.get("card") or item
            oracle_id = card.get("oracle_id") or ""
            if not oracle_id:
                continue
            name = card.get("name") or ""
            set_code = card.get("set") or ""
            cn = card.get("cn") or card.get("collector_number") or ""
            qty = int(item.get("quantity", 1))
            if exclude_basics and is_basic_land(oracle_id, name):
                logger.debug("[moxfield] wishlist items: excluding basic %r", name)
                continue
            effective_mode = printing_mode
            if printing_mode == PRINTING_MODE_EXACT and (not set_code or not cn):
                warnings.append(
                    f"Wishlist card {name!r} (oracle_id={oracle_id}) is missing "
                    f"set or collector_number; falling back to oracle_id match."
                )
                effective_mode = PRINTING_MODE_ANY
            cards.append({
                "oracle_id":        oracle_id,
                "name":             name,
                "quantity":         qty,
                "set":              set_code,
                "collector_number": cn,
                "scryfall_id":      card.get("scryfall_id") or "",
                "printing_mode":    effective_mode,
            })
        return cards, warnings

    # Nothing found — return empty (not an error; user may have empty wishlist).
    return [], []


def get_cached_deck(
    deck_id: str,
    printing_mode: str = PRINTING_MODE_ANY,
) -> Optional[dict]:
    """Return the cached deck payload from moxfield_decks, or None.

    Does NOT call the network.  The return value is the structured payload
    that the import endpoint returns — not the raw JSON blob.

    Args:
        deck_id: the Moxfield deck ID.
        printing_mode: "any" or "exact"; applied when re-parsing the cached
                       raw_json.  Defaults to "any" (matches v1 behaviour).
    """
    conn = enrichment_db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM moxfield_decks WHERE deck_id = ?",
            (deck_id,),
        ).fetchone()
        if row is None:
            return None
        raw = row["raw_json"]
        if not raw:
            return None
        data = json.loads(raw)
        cards, warnings = parse_deck_cards(data, printing_mode=printing_mode)
        return _format_import_result(deck_id, data, cards,
                                     printing_mode=printing_mode,
                                     warnings=warnings)
    finally:
        conn.close()


def get_cached_wishlist(
    username: str,
    printing_mode: str = PRINTING_MODE_ANY,
) -> Optional[dict]:
    """Return cached wishlist cards for a username, or None if not cached.

    Reads from moxfield_wishlists table.  Does NOT call the network.

    Args:
        username: the Moxfield username.
        printing_mode: "any" or "exact".

    Returns:
        Dict with keys: username, card_count, printing_mode, cards.
        Returns None if no rows exist for this username.
    """
    conn = enrichment_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM moxfield_wishlists WHERE username = ?",
            (username,),
        ).fetchall()
        if not rows:
            return None
        cards = []
        for r in rows:
            # Re-apply printing_mode fallback logic at read time.
            set_code = r["set_code"] or ""
            cn = r["collector_number"] or ""
            effective_mode = printing_mode
            if printing_mode == PRINTING_MODE_EXACT and (not set_code or not cn):
                effective_mode = PRINTING_MODE_ANY
            cards.append({
                "oracle_id":        r["oracle_id"],
                "name":             r["name"] or "",
                "quantity":         r["quantity"],
                "set":              set_code,
                "collector_number": cn,
                "scryfall_id":      r["scryfall_id"] or "",
                "printing_mode":    effective_mode,
            })
        return _format_wishlist_result(username, cards, printing_mode=printing_mode)
    finally:
        conn.close()


def cache_deck(deck_id: str, data: dict) -> None:
    """Write or update a deck in the moxfield_decks table.

    Uses a transaction so the row is never partially written.
    """
    owner = (data.get("createdByUser") or {}).get("userName") or ""
    deck_name = data.get("name") or ""
    fmt = data.get("format") or ""
    now_ts = int(time.time())

    conn = enrichment_db.get_connection()
    try:
        with conn:  # auto-commit / auto-rollback on exception
            conn.execute(
                """INSERT INTO moxfield_decks
                       (deck_id, deck_name, owner, last_fetched_at, format, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(deck_id) DO UPDATE SET
                       deck_name       = excluded.deck_name,
                       owner           = excluded.owner,
                       last_fetched_at = excluded.last_fetched_at,
                       format          = excluded.format,
                       raw_json        = excluded.raw_json""",
                (deck_id, deck_name, owner, now_ts, fmt, json.dumps(data)),
            )
    finally:
        conn.close()


def cache_wishlist(username: str, cards: list[dict]) -> None:
    """Atomically replace the cached wishlist for a username.

    Uses a DELETE + INSERT in a single transaction so the cache is never
    left in a partial state.

    Args:
        username: the Moxfield username (case-sensitive as returned by the API).
        cards: list of card dicts as returned by parse_wishlist_cards().
               Each dict must have: oracle_id, name, quantity, set,
               collector_number, scryfall_id.
    """
    now_ts = int(time.time())
    rows = [
        (
            username,
            c["oracle_id"],
            c.get("name") or "",
            int(c.get("quantity", 1)),
            c.get("set") or "",
            c.get("collector_number") or "",
            c.get("scryfall_id") or "",
            now_ts,
        )
        for c in cards
    ]
    conn = enrichment_db.get_connection()
    try:
        with conn:  # single transaction
            conn.execute(
                "DELETE FROM moxfield_wishlists WHERE username = ?",
                (username,),
            )
            if rows:
                conn.executemany(
                    """INSERT INTO moxfield_wishlists
                           (username, oracle_id, name, quantity, set_code,
                            collector_number, scryfall_id, last_fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
    finally:
        conn.close()


def _format_import_result(
    deck_id: str,
    data: dict,
    cards: list[dict],
    printing_mode: str = PRINTING_MODE_ANY,
    warnings: Optional[list[str]] = None,
) -> dict:
    """Build the structured response payload returned by the deck import endpoint."""
    return {
        "deck_id":       deck_id,
        "deck_name":     data.get("name") or "",
        "format":        data.get("format") or "",
        "owner":         (data.get("createdByUser") or {}).get("userName") or "",
        "printing_mode": printing_mode,
        "card_count":    sum(c["quantity"] for c in cards),
        "warnings":      warnings or [],
        "cards": [
            {
                "oracle_id":        c["oracle_id"],
                "name":             c["name"],
                "quantity":         c["quantity"],
                "set":              c["set"],
                "collector_number": c["collector_number"],
                "scryfall_id":      c["scryfall_id"],
                "board":            c.get("board", ""),
                "printing_mode":    c.get("printing_mode", printing_mode),
            }
            for c in cards
        ],
    }


def _format_wishlist_result(
    username: str,
    cards: list[dict],
    printing_mode: str = PRINTING_MODE_ANY,
    warnings: Optional[list[str]] = None,
) -> dict:
    """Build the structured response payload returned by the wishlist import endpoint."""
    return {
        "username":      username,
        "printing_mode": printing_mode,
        "card_count":    sum(c.get("quantity", 1) for c in cards),
        "warnings":      warnings or [],
        "cards": [
            {
                "oracle_id":        c["oracle_id"],
                "name":             c.get("name") or "",
                "quantity":         c.get("quantity", 1),
                "set":              c.get("set") or "",
                "collector_number": c.get("collector_number") or "",
                "scryfall_id":      c.get("scryfall_id") or "",
                "printing_mode":    c.get("printing_mode", printing_mode),
            }
            for c in cards
        ],
    }


# ---------------------------------------------------------------------------
# EnrichmentRepo helpers: query by oracle_id / (set, cn) for sort evaluator
# ---------------------------------------------------------------------------

def is_in_deck(
    deck_id: str,
    oracle_id: str,
    printing_mode: str = PRINTING_MODE_ANY,
    set_code: str = "",
    collector_number: str = "",
) -> bool:
    """Return True if oracle_id (or set+cn) is in the cached deck.

    Args:
        deck_id: Moxfield deck ID.
        oracle_id: the card's oracle_id.
        printing_mode: "any" → match by oracle_id only.
                       "exact" → match by (set_code, collector_number).
        set_code, collector_number: used only for exact mode.
    """
    conn = enrichment_db.get_connection()
    try:
        row = conn.execute(
            "SELECT raw_json FROM moxfield_decks WHERE deck_id = ?",
            (deck_id,),
        ).fetchone()
        if row is None or not row["raw_json"]:
            return False
        data = json.loads(row["raw_json"])
        cards, _ = parse_deck_cards(data, printing_mode=printing_mode)
        for c in cards:
            if printing_mode == PRINTING_MODE_EXACT and set_code and collector_number:
                if (c["set"] == set_code
                        and c["collector_number"] == collector_number):
                    return True
            else:
                if c["oracle_id"] == oracle_id:
                    return True
        return False
    finally:
        conn.close()


def is_in_wishlist(
    username: str,
    oracle_id: str,
    printing_mode: str = PRINTING_MODE_ANY,
    set_code: str = "",
    collector_number: str = "",
) -> bool:
    """Return True if oracle_id (or set+cn) is in the cached wishlist.

    Args:
        username: Moxfield username (case-sensitive).
        oracle_id: the card's oracle_id.
        printing_mode: "any" → match by oracle_id; "exact" → set+cn.
        set_code, collector_number: used only for exact mode.
    """
    conn = enrichment_db.get_connection()
    try:
        if printing_mode == PRINTING_MODE_EXACT and set_code and collector_number:
            row = conn.execute(
                """SELECT 1 FROM moxfield_wishlists
                   WHERE username = ? AND set_code = ? AND collector_number = ?
                   LIMIT 1""",
                (username, set_code, collector_number),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT 1 FROM moxfield_wishlists
                   WHERE username = ? AND oracle_id = ?
                   LIMIT 1""",
                (username, oracle_id),
            ).fetchone()
        return row is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# EnrichmentSource implementation
# ---------------------------------------------------------------------------

class MoxfieldSource(EnrichmentSource):
    """On-demand Moxfield deck and wishlist importer.

    Unlike other EnrichmentSource implementations, this source is never
    registered with a cron schedule.  It is invoked directly from the
    /api/integrations/moxfield/deck/import and
    /api/integrations/moxfield/wishlist/import endpoints (or from tests)
    via import_deck() and import_wishlist().

    probe() and refresh() are implemented for interface compliance and for
    health-check tooling.  probe() performs a lightweight API hit against the
    canonical probe deck ID; refresh() is a no-op (the concept of a "full
    refresh" of all Moxfield decks is deferred to item 3.18).
    """

    name = SOURCE

    # ------------------------------------------------------------------
    # EnrichmentSource contract
    # ------------------------------------------------------------------

    def probe(self) -> bool:
        """Delegate to probes.probe_moxfield.probe()."""
        from probes.probe_moxfield import probe as _probe
        r = _probe()
        return r.ok

    def refresh(
        self,
        emit: Optional[EmitFn] = None,
        full: bool = False,
    ) -> RefreshResult:
        """Not scheduled.  Returns a warning but does not fail hard.

        A future item (3.18 deck-usage overlay) will wire a real refresh
        that re-pulls all cached decks whose last_fetched_at is stale.
        For now this is a no-op stub that keeps the scheduler happy.
        """
        start = time.time()
        warning = (
            "moxfield: full refresh not implemented in v1.  "
            "Use import_deck() for individual deck imports."
        )
        logger.info("[moxfield] %s", warning)

        conn = enrichment_db.get_connection()
        try:
            enrichment_db.record_sync_attempt(
                conn,
                source=self.name,
                success=True,          # not a failure — intentionally no-op
                coverage_pct=0.0,
            )
        finally:
            conn.close()

        return RefreshResult(
            source=self.name,
            success=True,
            duration_ms=int((time.time() - start) * 1000),
            rows_changed=0,
            coverage_pct=0.0,
            warnings=[warning],
        )

    def coverage_report(self) -> dict:
        """Count cached decks and wishlists; read-only, no network."""
        conn = enrichment_db.get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM moxfield_decks"
            ).fetchone()
            cached_decks = row["cnt"] if row else 0

            row2 = conn.execute(
                "SELECT COUNT(DISTINCT username) AS cnt FROM moxfield_wishlists"
            ).fetchone()
            cached_wishlists = row2["cnt"] if row2 else 0

            meta = conn.execute(
                "SELECT * FROM sync_metadata WHERE source = ?",
                (self.name,),
            ).fetchone()
            base = dict(meta) if meta else {
                "source": self.name,
                "last_success": None,
                "last_attempt": None,
                "error": None,
                "coverage_pct": 0.0,
            }
            base["cached_decks"] = cached_decks
            base["cached_wishlists"] = cached_wishlists
            return base
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Primary API — used by web_server.py endpoints
    # ------------------------------------------------------------------

    def import_deck(
        self,
        url_or_id: str,
        *,
        include_side: bool = False,
        use_cache: bool = True,
        printing_mode: str = PRINTING_MODE_ANY,
    ) -> dict:
        """Fetch a public deck and cache it.  Returns the import payload.

        Args:
            url_or_id: full Moxfield URL or bare deck_id string.
            include_side: also include sideboard cards in card list.
            use_cache: if True and the deck is already cached, return cached
                       data without a network call.
            printing_mode: "any" (default) or "exact".  Controls whether cards
                           are matched by oracle_id or by (set, cn).

        Returns:
            Dict with keys: deck_id, deck_name, format, owner, printing_mode,
            card_count, warnings, cards (list).

        Raises:
            ValueError — URL/ID could not be parsed, mode invalid, or API
                         response invalid.
            httpx.HTTPStatusError — non-2xx from Moxfield (e.g. private deck).
            httpx.RequestError — network failure.
        """
        if printing_mode not in VALID_PRINTING_MODES:
            raise ValueError(
                f"Invalid printing_mode {printing_mode!r}. "
                f"Must be one of: {VALID_PRINTING_MODES}"
            )

        deck_id = extract_deck_id(url_or_id)
        if deck_id is None:
            raise ValueError(
                f"Could not extract a Moxfield deck_id from {url_or_id!r}.  "
                "Expected a URL like https://www.moxfield.com/decks/<id> or "
                "a bare deck ID string."
            )

        if use_cache:
            cached = get_cached_deck(deck_id, printing_mode=printing_mode)
            if cached is not None:
                logger.debug("[moxfield] cache hit for deck_id=%s", deck_id)
                return cached

        data = fetch_deck(deck_id)
        cards, warnings = parse_deck_cards(
            data,
            include_side=include_side,
            printing_mode=printing_mode,
        )
        cache_deck(deck_id, data)
        return _format_import_result(deck_id, data, cards,
                                     printing_mode=printing_mode,
                                     warnings=warnings)

    def import_wishlist(
        self,
        username: str,
        *,
        use_cache: bool = True,
        printing_mode: str = PRINTING_MODE_ANY,
    ) -> dict:
        """Fetch a user's public wishlist and cache it.

        Args:
            username: Moxfield username (no URL — just the username string).
            use_cache: if True and the wishlist is already cached, return
                       cached data without a network call.
            printing_mode: "any" (default) or "exact".

        Returns:
            Dict with keys: username, printing_mode, card_count, warnings,
            cards (list of {oracle_id, name, quantity, set, collector_number,
            scryfall_id, printing_mode}).

        Raises:
            ValueError — invalid printing_mode or API response shape.
            httpx.HTTPStatusError — non-2xx from Moxfield (e.g. 401 private
                                     wishlist, 404 user not found).
            httpx.RequestError — network failure.
        """
        if printing_mode not in VALID_PRINTING_MODES:
            raise ValueError(
                f"Invalid printing_mode {printing_mode!r}. "
                f"Must be one of: {VALID_PRINTING_MODES}"
            )

        username = username.strip()
        if not username:
            raise ValueError("username must not be empty")

        if use_cache:
            cached = get_cached_wishlist(username, printing_mode=printing_mode)
            if cached is not None:
                logger.debug("[moxfield] cache hit for wishlist username=%s", username)
                return cached

        data = fetch_wishlist(username)
        cards, warnings = parse_wishlist_cards(data, printing_mode=printing_mode)
        cache_wishlist(username, cards)
        return _format_wishlist_result(username, cards,
                                       printing_mode=printing_mode,
                                       warnings=warnings)
