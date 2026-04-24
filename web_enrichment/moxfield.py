# web_enrichment/moxfield.py
# ---------------------------------------------------------------------------
# MoxfieldSource: import a single public Moxfield deck by URL and cache the
# result in the `moxfield_decks` table inside enrichment.db.
#
# This is NOT a scheduled background source like EDHREC or buylists.  It is
# triggered on demand when the user pastes a public deck URL into the UI.
# The EnrichmentSource contract is implemented so the class slots cleanly into
# the existing scheduler / coverage plumbing, but it is never registered with
# RefreshScheduler (it has no cron schedule).
#
# ## API endpoint
#
# Empirical findings (verified 2026-04-23 against probe_moxfield.py output):
#
#   GET https://api2.moxfield.com/v2/decks/all/<deck_id>
#
# Returns a JSON object with the keys listed in REQUIRED_KEYS below.  The
# alternative v3 path mentioned in some community tools (api2.moxfield.com/v3/)
# returns 404 — v2 is the current stable path.
#
# ## Deck-card extraction
#
# `mainboard` + `commanders` are merged into the canonical deck list.
# `sideboard` and `maybeboard` are skipped by default; pass
# `include_side=True` to merge sideboard as well.
#
# Each slot looks like:
#   { "quantity": N, "card": { "oracle_id": "...", "name": "...", "set": "...",
#                               "cn": "...", "scryfall_id": "..." } }
#
# ## Preset generation (future work — item 3.16)
#
# The `refresh()` method returns enough data for the UI to build a sort preset:
#   - `printing_mode: any`    → match by oracle_id only
#   - `printing_mode: exact`  → match by (set, collector_number)
#   - `printing_mode: prefer_listed` → two-pass preset: exact first, oracle fallback
#   - `target_bin_strategy: single_bin` / `by_category`
#
# These modes are documented here for when item 3.16 builds the preset-creation
# UI.  This file's job is fetching, caching, and returning structured card data.
#
# ## v1 constraints
#
# * Public decks only.  No OAuth / bearer token.  (Auth is item 3.16.)
# * No Moxfield user account config — the user pastes a URL per deck.
# * Rate limit: 1 req/deck, no pagination needed (full deck in one response).
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


def parse_deck_cards(
    data: dict,
    *,
    include_side: bool = False,
) -> list[dict]:
    """Parse a Moxfield deck payload into a flat card list.

    Args:
        data: full decoded JSON from the Moxfield API.
        include_side: if True, include sideboard cards in the output.

    Returns:
        List of dicts:
          { oracle_id, name, quantity, set, collector_number, scryfall_id,
            board }
        `board` is one of: "mainboard", "commanders", "sideboard".
        Only cards whose `card.oracle_id` is non-empty are returned.
    """
    boards: list[tuple[str, dict]] = [
        ("mainboard",  data.get("mainboard",  {})),
        ("commanders", data.get("commanders", {})),
    ]
    if include_side:
        boards.append(("sideboard", data.get("sideboard", {})))

    cards: list[dict] = []
    for board_name, board in boards:
        for _key, slot in board.items():
            card = slot.get("card") or {}
            oracle_id = card.get("oracle_id") or ""
            if not oracle_id:
                continue
            cards.append({
                "oracle_id":        oracle_id,
                "name":             card.get("name") or "",
                "quantity":         int(slot.get("quantity", 1)),
                "set":              card.get("set") or "",
                "collector_number": card.get("cn") or "",
                "scryfall_id":      card.get("scryfall_id") or "",
                "board":            board_name,
            })
    return cards


def get_cached_deck(deck_id: str) -> Optional[dict]:
    """Return the cached deck payload from moxfield_decks, or None.

    Does NOT call the network.  The return value is the structured payload
    that the import endpoint returns — not the raw JSON blob.
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
        cards = parse_deck_cards(data)
        return _format_import_result(deck_id, data, cards)
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


def _format_import_result(deck_id: str, data: dict, cards: list[dict]) -> dict:
    """Build the structured response payload returned by the import endpoint."""
    return {
        "deck_id":    deck_id,
        "deck_name":  data.get("name") or "",
        "format":     data.get("format") or "",
        "owner":      (data.get("createdByUser") or {}).get("userName") or "",
        "card_count": sum(c["quantity"] for c in cards),
        "cards":      [
            {
                "oracle_id":        c["oracle_id"],
                "name":             c["name"],
                "quantity":         c["quantity"],
                "set":              c["set"],
                "collector_number": c["collector_number"],
                "scryfall_id":      c["scryfall_id"],
                "board":            c["board"],
            }
            for c in cards
        ],
    }


# ---------------------------------------------------------------------------
# EnrichmentSource implementation
# ---------------------------------------------------------------------------

class MoxfieldSource(EnrichmentSource):
    """On-demand Moxfield deck importer.

    Unlike other EnrichmentSource implementations, this source is never
    registered with a cron schedule.  It is invoked directly from the
    /api/integrations/moxfield/deck/import endpoint (or from tests) via
    import_deck().

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
        """Count cached decks; read-only, no network."""
        conn = enrichment_db.get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM moxfield_decks"
            ).fetchone()
            cached = row["cnt"] if row else 0

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
            base["cached_decks"] = cached
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
    ) -> dict:
        """Fetch a public deck and cache it.  Returns the import payload.

        Args:
            url_or_id: full Moxfield URL or bare deck_id string.
            include_side: also include sideboard cards in card list.
            use_cache: if True and the deck is already cached, return cached
                       data without a network call.

        Returns:
            Dict with keys: deck_id, deck_name, format, owner, card_count,
            cards (list of {oracle_id, name, quantity, set, collector_number,
            scryfall_id, board}).

        Raises:
            ValueError — URL/ID could not be parsed, or API response invalid.
            httpx.HTTPStatusError — non-2xx from Moxfield (e.g. private deck).
            httpx.RequestError — network failure.
        """
        deck_id = extract_deck_id(url_or_id)
        if deck_id is None:
            raise ValueError(
                f"Could not extract a Moxfield deck_id from {url_or_id!r}.  "
                "Expected a URL like https://www.moxfield.com/decks/<id> or "
                "a bare deck ID string."
            )

        if use_cache:
            cached = get_cached_deck(deck_id)
            if cached is not None:
                logger.debug("[moxfield] cache hit for deck_id=%s", deck_id)
                return cached

        data = fetch_deck(deck_id)
        cards = parse_deck_cards(data, include_side=include_side)
        cache_deck(deck_id, data)
        return _format_import_result(deck_id, data, cards)
