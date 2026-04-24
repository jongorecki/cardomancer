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

    def is_cull_candidate(self, oracle_id: str, oracle_text: str = "") -> bool:
        """Return True if this oracle_id is a dead-weight cull candidate.

        Cull criteria (ALL must hold):
          1. Vanilla or french-vanilla oracle text.
          2. No staple row in the staples table at any tier.
          3. No CK buylist entry with price > 0.
          4. Not in any of the user's Moxfield decks.
             TODO: AND with deck-usage lookup once Phase 3 item 3.18
             (Moxfield deck-usage overlay) is implemented.  For now treated
             as trivially satisfied (not in any deck) since the deck_usage
             table does not yet exist.

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

            # TODO: check deck_usage table once Phase 3 item 3.18 lands.
            # deck_row = conn.execute(
            #     "SELECT 1 FROM deck_usage WHERE oracle_id = ? LIMIT 1",
            #     (oracle_id,),
            # ).fetchone()
            # if deck_row is not None:
            #     return False

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
