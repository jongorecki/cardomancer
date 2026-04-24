# web_enrichment/repo.py
# ---------------------------------------------------------------------------
# Read-only access layer over enrichment.db. Every feature that needs
# enrichment data goes through this class; never write raw SQL against
# enrichment tables outside of this module and the concrete
# EnrichmentSource implementations.
#
# Phase 0A provides the interface + simple lookups (get_card, get_tags,
# get_staples, coverage_overview). Query-language evaluation
# (repo.query()) is a stub here; it's fully implemented in Phase 2 once
# the enrichment-aware query_parser tokens land.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from enrichment_db import get_connection as get_enrichment_connection


@dataclass
class CardEnrichment:
    """Full enrichment payload for one oracle_id."""

    oracle_id: str
    tags: list[str] = field(default_factory=list)
    art_tags_by_printing: dict[str, list[str]] = field(default_factory=dict)
    staples: dict = field(default_factory=dict)
    salt: Optional[float] = None
    combos: list[dict] = field(default_factory=list)
    buylists: dict[str, float] = field(default_factory=dict)
    themes: list[str] = field(default_factory=list)
    commander_rank: Optional[int] = None
    source_freshness: dict[str, str] = field(default_factory=dict)


class EnrichmentRepo:
    """Read-only access to enrichment.db. Writers use EnrichmentSource."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

    # -- Connection helpers -------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        return get_enrichment_connection(db_path=self.db_path)

    # -- Card lookups -------------------------------------------------------

    def get_card(self, oracle_id: str) -> Optional[CardEnrichment]:
        """Full enrichment for one card. Returns None if oracle_id is
        unknown (not present in card_universe)."""
        conn = self._conn()
        try:
            universe_row = conn.execute(
                "SELECT oracle_id FROM card_universe WHERE oracle_id = ?",
                (oracle_id,),
            ).fetchone()
            if universe_row is None:
                return None

            tags = [
                r["tag_name"] for r in conn.execute(
                    "SELECT tag_name FROM tags WHERE oracle_id = ?",
                    (oracle_id,),
                )
            ]
            local_tags = [
                r["tag_name"] for r in conn.execute(
                    "SELECT tag_name FROM local_tags WHERE oracle_id = ?",
                    (oracle_id,),
                )
            ]
            merged_tags = sorted(set(tags) | set(local_tags))

            staple_rows = conn.execute(
                """SELECT tier, source, score, archetypes_json
                   FROM staples WHERE oracle_id = ?""",
                (oracle_id,),
            ).fetchall()
            staples: dict = {
                "universal": False,
                "archetype": False,
                "cedh": False,
                "archetype_count": 0,
                "archetypes": [],
            }
            for r in staple_rows:
                tier = r["tier"]
                if tier in staples:
                    staples[tier] = True
                if tier == "archetype" and r["archetypes_json"]:
                    try:
                        arches = json.loads(r["archetypes_json"])
                        if isinstance(arches, list):
                            staples["archetypes"] = arches
                            staples["archetype_count"] = len(arches)
                    except (ValueError, TypeError):
                        pass

            salt_row = conn.execute(
                "SELECT salt FROM salt_scores WHERE oracle_id = ?",
                (oracle_id,),
            ).fetchone()
            salt = salt_row["salt"] if salt_row else None

            combo_rows = conn.execute(
                """SELECT c.combo_id, c.result, c.identity, c.mana_needed
                   FROM combo_membership cm
                   JOIN combos c ON c.combo_id = cm.combo_id
                   WHERE cm.oracle_id = ?""",
                (oracle_id,),
            ).fetchall()
            combos = [dict(r) for r in combo_rows]

            buylist_rows = conn.execute(
                """SELECT vendor, price_usd FROM buylists
                   WHERE oracle_id = ?""",
                (oracle_id,),
            ).fetchall()
            buylists = {
                r["vendor"]: r["price_usd"]
                for r in buylist_rows
                if r["price_usd"] is not None
            }

            theme_rows = conn.execute(
                """SELECT DISTINCT theme_name FROM themes
                   WHERE oracle_id = ? ORDER BY theme_name""",
                (oracle_id,),
            ).fetchall()
            themes = [r["theme_name"] for r in theme_rows]

            rank_row = conn.execute(
                """SELECT deck_count FROM commander_ranks
                   WHERE oracle_id = ?""",
                (oracle_id,),
            ).fetchone()
            commander_rank = rank_row["deck_count"] if rank_row else None

            freshness = self._compute_freshness(conn)

            return CardEnrichment(
                oracle_id=oracle_id,
                tags=merged_tags,
                art_tags_by_printing={},
                staples=staples,
                salt=salt,
                combos=combos,
                buylists=buylists,
                themes=themes,
                commander_rank=commander_rank,
                source_freshness=freshness,
            )
        finally:
            conn.close()

    def get_tags(self, oracle_id: str) -> list[str]:
        """Just the function tags for one oracle_id."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT tag_name FROM tags WHERE oracle_id = ?",
                (oracle_id,),
            )
            return [r["tag_name"] for r in rows]
        finally:
            conn.close()

    def get_staples(self, tier: str) -> list[str]:
        """Return oracle_ids for a staple tier.
        tier ∈ {universal, archetype, cedh}."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT oracle_id FROM staples WHERE tier = ?",
                (tier,),
            )
            return [r["oracle_id"] for r in rows]
        finally:
            conn.close()

    def get_salt_score(self, oracle_id: str) -> Optional[float]:
        """Return the EDHREC salt score for oracle_id, or None if unknown.

        Salt scores are populated by EDHRECSource from individual card pages.
        Returns None if no score has been loaded yet.
        """
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT salt FROM salt_scores WHERE oracle_id = ?",
                (oracle_id,),
            ).fetchone()
            return row["salt"] if row else None
        finally:
            conn.close()

    def get_commander_popularity(self, oracle_id: str) -> Optional[int]:
        """Return the deck_count (number of EDHREC decks) for a commander.

        Populated by EDHRECSource from the top-commanders page.  Non-commander
        cards will return None.  Returns None if not found.
        """
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT deck_count FROM commander_ranks WHERE oracle_id = ?",
                (oracle_id,),
            ).fetchone()
            return row["deck_count"] if row else None
        finally:
            conn.close()

    def get_staples_by_theme(self, theme_name: str) -> list[str]:
        """Return oracle_ids that are members of the given EDHREC theme.

        Reads from the themes table (populated by EDHRECSource).
        Returns an empty list if the theme is unknown or the source hasn't run.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT oracle_id FROM themes WHERE theme_name = ?",
                (theme_name,),
            )
            return [r["oracle_id"] for r in rows]
        finally:
            conn.close()

    def get_staples_by_color_or_theme(self, color_or_theme: str) -> list[str]:
        """Return oracle_ids associated with a color identity or theme.

        Searches two sources:
          1. themes table: cards with theme_name == color_or_theme
          2. staples table: cards with tier='archetype' whose archetypes_json
             contains color_or_theme as a list element (legacy named-list rows)

        Returns a deduplicated list. Query-parser tokens use this to resolve
        staple:archetype:<theme> queries.  No network calls made.
        """
        conn = self._conn()
        try:
            # Source 1: themes membership table (Phase 1.10 EDHRECSource output)
            theme_rows = conn.execute(
                "SELECT DISTINCT oracle_id FROM themes WHERE theme_name = ?",
                (color_or_theme,),
            ).fetchall()
            oids: set[str] = {r["oracle_id"] for r in theme_rows}

            # Source 2: staples.archetypes_json containing the slug as a list
            # element (only applies if archetypes_json holds a JSON array)
            archetype_rows = conn.execute(
                """SELECT oracle_id, archetypes_json FROM staples
                   WHERE tier = 'archetype' AND archetypes_json IS NOT NULL""",
            ).fetchall()
            for row in archetype_rows:
                try:
                    arches = json.loads(row["archetypes_json"])
                    if isinstance(arches, list) and color_or_theme in arches:
                        oids.add(row["oracle_id"])
                except (ValueError, TypeError):
                    pass

            return sorted(oids)
        finally:
            conn.close()

    def get_cedh_staples(self, min_play_rate: float = 0.0) -> list[dict]:
        """Return cEDH staples from edhtop16, filtered by minimum play rate.

        Args:
            min_play_rate: minimum score (0.0–1.0). Default returns all rows.

        Returns:
            List of dicts with keys: oracle_id, play_rate, tournament_appearances,
            last_refreshed.  Results ordered by play_rate descending.

        Note: ``tournament_appearances`` is not stored separately in the DB;
        it is None for all rows.  The ``play_rate`` field maps directly to the
        ``score`` column in the staples table (fraction of sampled decks that
        included the card, or playRateLastYear for the fallback path).
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT oracle_id, score, last_updated
                   FROM staples
                   WHERE source = 'edhtop16'
                     AND tier = 'cedh'
                     AND score >= ?
                   ORDER BY score DESC""",
                (min_play_rate,),
            ).fetchall()
            return [
                {
                    "oracle_id": r["oracle_id"],
                    "play_rate": r["score"],
                    "tournament_appearances": None,
                    "last_refreshed": r["last_updated"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_combo_members(self, combo_id: str) -> list[str]:
        """Oracle_ids that make up a combo."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT oracle_id FROM combo_membership
                   WHERE combo_id = ?""",
                (combo_id,),
            )
            return [r["oracle_id"] for r in rows]
        finally:
            conn.close()

    def buylist_ck_price(self, oracle_id: str) -> Optional[float]:
        """Return the CardKingdom buylist price for oracle_id, or None.

        Fast read from buylists table (no network). Returns None if the
        card has no CK buylist entry or the entry has a null price.
        """
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT price_usd FROM buylists "
                "WHERE oracle_id = ? AND vendor = 'ck'",
                (oracle_id,),
            ).fetchone()
            if row is None:
                return None
            return row["price_usd"]  # may be None if stored as NULL
        finally:
            conn.close()

    def get_moxfield_deck_cards(
        self,
        deck_id: str,
        printing_mode: str = "any",
    ) -> list[dict]:
        """Return cached card list for a Moxfield deck.

        Args:
            deck_id: the Moxfield deck ID.
            printing_mode: "any" or "exact"; applied when re-parsing cached data.

        Returns:
            List of card dicts (same shape as import endpoint `cards` key).
            Empty list if the deck is not cached.
        """
        from web_enrichment.moxfield import get_cached_deck
        result = get_cached_deck(deck_id, printing_mode=printing_mode)
        if result is None:
            return []
        return result.get("cards", [])

    def get_moxfield_wishlist_cards(
        self,
        username: str,
        printing_mode: str = "any",
    ) -> list[dict]:
        """Return cached card list for a Moxfield user's wishlist.

        Args:
            username: Moxfield username (case-sensitive).
            printing_mode: "any" or "exact".

        Returns:
            List of card dicts (same shape as wishlist import endpoint
            `cards` key).  Empty list if wishlist not cached.
        """
        from web_enrichment.moxfield import get_cached_wishlist
        result = get_cached_wishlist(username, printing_mode=printing_mode)
        if result is None:
            return []
        return result.get("cards", [])

    def is_used_in_deck(self, oracle_id: str) -> bool:
        """Return True if oracle_id appears in any cached Moxfield deck or
        wishlist (deck_count > 0 OR wishlist_count > 0 in deck_usage).

        Reads from the deck_usage overlay table populated by
        refresh_deck_usage().  Returns False if the table is empty or if
        the oracle_id has no row (i.e. the card is not in any imported
        deck/wishlist).
        """
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT deck_count, wishlist_count FROM deck_usage "
                "WHERE oracle_id = ?",
                (oracle_id,),
            ).fetchone()
            if row is None:
                return False
            return (row["deck_count"] > 0) or (row["wishlist_count"] > 0)
        except Exception:
            # deck_usage table may not exist in very old DB files; degrade
            # gracefully rather than crashing.
            return False
        finally:
            conn.close()

    def refresh_deck_usage(self) -> int:
        """Rebuild the deck_usage overlay from all cached Moxfield decks and
        wishlists.  Returns the number of oracle_id rows written.

        Delegates to enrichment_db.rebuild_deck_usage() which does the
        aggregation in a single atomic transaction.
        """
        import enrichment_db as _edb
        conn = self._conn()
        try:
            return _edb.rebuild_deck_usage(conn)
        finally:
            conn.close()

    def is_cull_candidate(self, oracle_id: str, oracle_text: str = "") -> bool:
        """Return True if this oracle_id is a dead-weight cull candidate.

        Cull criteria (ALL must hold):
          1. Vanilla or french-vanilla oracle text.
          2. No staple row in the staples table at any tier.
          3. No CK buylist entry with price > 0.
          4. Not in any of the user's Moxfield decks or wishlists
             (deck_usage.deck_count == 0 AND deck_usage.wishlist_count == 0).

        Parameters
        ----------
        oracle_id:
            Scryfall oracle_id for the card.
        oracle_text:
            The card's oracle text (Scryfall ``oracle_text`` field).
            Pass ``""`` for textless / vanilla cards.
        """
        from web_enrichment.vanilla import is_vanilla_or_french_vanilla
        if not is_vanilla_or_french_vanilla(oracle_text):
            return False

        conn = self._conn()
        try:
            staple_row = conn.execute(
                "SELECT 1 FROM staples WHERE oracle_id = ? LIMIT 1",
                (oracle_id,),
            ).fetchone()
            if staple_row is not None:
                return False

            buylist_row = conn.execute(
                "SELECT price_usd FROM buylists "
                "WHERE oracle_id = ? AND vendor = 'ck' LIMIT 1",
                (oracle_id,),
            ).fetchone()
            if buylist_row is not None and buylist_row["price_usd"] \
                    and buylist_row["price_usd"] > 0:
                return False

            # Predicate 4: NOT in any of the user's Moxfield decks/wishlists.
            # Reads from the deck_usage overlay (populated by
            # refresh_deck_usage / rebuild_deck_usage).  Degrades gracefully
            # if the table is absent (treated as "not in any deck").
            try:
                deck_row = conn.execute(
                    "SELECT deck_count, wishlist_count FROM deck_usage "
                    "WHERE oracle_id = ? LIMIT 1",
                    (oracle_id,),
                ).fetchone()
                if deck_row is not None and (
                        deck_row["deck_count"] > 0
                        or deck_row["wishlist_count"] > 0):
                    return False
            except Exception:
                pass  # table absent → treat as not in any deck

            return True
        finally:
            conn.close()

    # -- Coverage -----------------------------------------------------------

    def coverage_overview(self) -> dict:
        """Per-source coverage for /api/enrichment/sources.
        Reads sync_metadata; never does network work."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM sync_metadata ORDER BY source"
            ).fetchall()
            return {r["source"]: dict(r) for r in rows}
        finally:
            conn.close()

    # -- Query (Phase 2 stub) -----------------------------------------------

    def query(self, scryfall_query: str, *, limit: int = 50,
              offset: int = 0, owned_only: bool = False
              ) -> tuple[list[dict], int]:
        """Run an enrichment-aware query (supports otag:, atag:, staple:,
        salt>, combo:). Returns (cards, total_count).

        Phase 0A stub: returns empty list. Full implementation lands in
        Phase 2 once query_parser gains the new tokens.
        """
        return [], 0

    # -- Internals ----------------------------------------------------------

    def _compute_freshness(self, conn: sqlite3.Connection) -> dict[str, str]:
        """Compute per-source freshness label from sync_metadata."""
        from datetime import datetime, timezone

        out: dict[str, str] = {}
        rows = conn.execute(
            "SELECT source, last_success FROM sync_metadata"
        ).fetchall()
        now = datetime.now(timezone.utc)
        for r in rows:
            ls = r["last_success"]
            if not ls:
                out[r["source"]] = "missing"
                continue
            try:
                ts = datetime.fromisoformat(ls)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                out[r["source"]] = "missing"
                continue
            age_days = (now - ts).days
            if age_days <= 7:
                out[r["source"]] = "fresh"
            else:
                out[r["source"]] = f"stale_{age_days}d"
        return out
