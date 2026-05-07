# enrichment_db.py
# ---------------------------------------------------------------------------
# SQLite-based enrichment database. Keyed on the full Scryfall oracle
# corpus (~30k oracle_ids), NOT just owned cards. All enrichment pulls
# from external sources (Scryfall Tagger, EDHREC, edhtop16, Commander
# Spellbook, buylists) land here.
#
# Collection queries against owned cards do a LEFT JOIN of collection.db
# against this DB on oracle_id — clean, no foreign keys across DBs.
#
# Schema is canonical per plans/handoff/07_shared_interfaces.md.
# Migrations are idempotent: every CREATE uses IF NOT EXISTS.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from typing import Optional

from config import SCRIPT_DIR

DB_PATH = os.path.join(SCRIPT_DIR, "enrichment.db")


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open (and migrate) the enrichment database. Returns a sqlite3
    Connection configured with WAL + Row factory to match collection_db."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create all enrichment tables + indices. Idempotent."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tags (
            oracle_id TEXT NOT NULL,
            tag_name TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (oracle_id, tag_name)
        );
        CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag_name);

        CREATE TABLE IF NOT EXISTS art_tags (
            printing_id TEXT NOT NULL,
            tag_name TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (printing_id, tag_name)
        );
        CREATE INDEX IF NOT EXISTS idx_art_tags_tag ON art_tags(tag_name);

        CREATE TABLE IF NOT EXISTS tag_catalog (
            tag_name TEXT PRIMARY KEY,
            tag_type TEXT NOT NULL,
            parent TEXT,
            description TEXT,
            card_count_expected INTEGER,
            source TEXT NOT NULL,
            last_updated TEXT
        );

        -- otag_relations and tag_catalog.cluster_id were the storage
        -- backing the Otag Explorer UI (Galaxy / Outline / Tree / Atlas
        -- modes), which was removed in Phase 1a along with its scraper
        -- and graph-derivation modules. Older DBs may still have the
        -- orphan table / column — they're harmless but no longer
        -- populated or read by any code path.

        CREATE TABLE IF NOT EXISTS staples (
            oracle_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            source TEXT NOT NULL,
            score REAL,
            archetypes_json TEXT,
            last_updated TEXT,
            PRIMARY KEY (oracle_id, tier, source)
        );
        CREATE INDEX IF NOT EXISTS idx_staples_tier ON staples(tier);

        CREATE TABLE IF NOT EXISTS salt_scores (
            oracle_id TEXT PRIMARY KEY,
            salt REAL,
            last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS themes (
            oracle_id TEXT NOT NULL,
            theme_name TEXT NOT NULL,
            source TEXT NOT NULL,
            inclusion_pct REAL,
            PRIMARY KEY (oracle_id, theme_name, source)
        );
        CREATE INDEX IF NOT EXISTS idx_themes_theme ON themes(theme_name);

        CREATE TABLE IF NOT EXISTS combos (
            combo_id TEXT PRIMARY KEY,
            result TEXT,
            identity TEXT,
            mana_needed TEXT,
            prerequisites_json TEXT,
            source TEXT NOT NULL DEFAULT 'spellbook'
        );

        CREATE TABLE IF NOT EXISTS combo_membership (
            oracle_id TEXT NOT NULL,
            combo_id TEXT NOT NULL,
            quantity INTEGER DEFAULT 1,
            PRIMARY KEY (oracle_id, combo_id),
            FOREIGN KEY (combo_id) REFERENCES combos(combo_id)
        );
        CREATE INDEX IF NOT EXISTS idx_combo_membership_oracle
            ON combo_membership(oracle_id);

        CREATE TABLE IF NOT EXISTS commander_ranks (
            oracle_id TEXT PRIMARY KEY,
            deck_count INTEGER,
            avg_synergy_json TEXT,
            source TEXT NOT NULL,
            last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS buylists (
            oracle_id TEXT NOT NULL,
            vendor TEXT NOT NULL,
            price_usd REAL,
            last_updated TEXT,
            PRIMARY KEY (oracle_id, vendor)
        );

        CREATE TABLE IF NOT EXISTS price_history (
            oracle_id TEXT NOT NULL,
            date TEXT NOT NULL,
            market_usd REAL,
            source TEXT NOT NULL,
            PRIMARY KEY (oracle_id, date, source)
        );

        CREATE TABLE IF NOT EXISTS local_tags (
            oracle_id TEXT NOT NULL,
            tag_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (oracle_id, tag_name)
        );

        CREATE TABLE IF NOT EXISTS sync_metadata (
            source TEXT PRIMARY KEY,
            last_success TEXT,
            last_attempt TEXT,
            version_hash TEXT,
            error TEXT,
            coverage_pct REAL
        );

        CREATE TABLE IF NOT EXISTS coverage_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            run_at TEXT NOT NULL,
            key_name TEXT,
            expected INTEGER,
            actual INTEGER,
            diff_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_coverage_source
            ON coverage_reports(source, run_at);

        CREATE TABLE IF NOT EXISTS card_universe (
            oracle_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            last_seen_bulk TEXT
        );

        -- Moxfield deck cache. Populated by MoxfieldSource.refresh().
        -- raw_json stores the full API response for offline re-parsing.
        CREATE TABLE IF NOT EXISTS moxfield_decks (
            deck_id      TEXT PRIMARY KEY,
            deck_name    TEXT,
            owner        TEXT,
            last_fetched_at INTEGER,
            format       TEXT,
            raw_json     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_moxfield_decks_owner
            ON moxfield_decks(owner);

        -- Moxfield wishlist cache. One row per card in the wishlist.
        -- Each row stores both oracle_id (for any-printing matching) and
        -- set/collector_number (for exact-printing matching).
        -- Repopulated atomically on each wishlist import (DELETE + INSERT in
        -- a single transaction so partial-commit is impossible).
        CREATE TABLE IF NOT EXISTS moxfield_wishlists (
            username         TEXT NOT NULL,
            oracle_id        TEXT NOT NULL,
            name             TEXT,
            quantity         INTEGER NOT NULL DEFAULT 1,
            set_code         TEXT,
            collector_number TEXT,
            scryfall_id      TEXT,
            last_fetched_at  INTEGER,
            PRIMARY KEY (username, oracle_id)
        );
        CREATE INDEX IF NOT EXISTS idx_moxfield_wishlists_username
            ON moxfield_wishlists(username);
        CREATE INDEX IF NOT EXISTS idx_moxfield_wishlists_oracle
            ON moxfield_wishlists(oracle_id);

        -- Deck-usage overlay. Populated by rebuild_deck_usage().
        -- Aggregates moxfield_decks + moxfield_wishlists into per-oracle_id
        -- counts so is_cull_candidate() / is_used_in_deck() can check
        -- membership in O(1).  Rebuilt atomically (DELETE + INSERT in a
        -- single transaction) after every deck or wishlist import and on
        -- demand via EnrichmentRepo.refresh_deck_usage().
        CREATE TABLE IF NOT EXISTS deck_usage (
            oracle_id      TEXT PRIMARY KEY,
            deck_count     INTEGER NOT NULL DEFAULT 0,
            wishlist_count INTEGER NOT NULL DEFAULT 0,
            updated_at     TEXT NOT NULL
        );
    """)
    conn.commit()
    _migrate_existing_schema(conn)


def _migrate_existing_schema(conn: sqlite3.Connection) -> None:
    """Additive column migrations for existing databases.

    Called after the CREATE IF NOT EXISTS block so that databases created
    before a schema addition get the new columns without losing data.
    Each ALTER TABLE is wrapped in a try/except — SQLite raises if the
    column already exists, which is the expected case after the first run.
    """
    _add_column_if_missing(
        conn, "tag_catalog", "last_updated", "TEXT"
    )
    # cluster_id was added for the deleted Otag Explorer's Atlas mode.
    # Don't add it on fresh DBs; older DBs that already have the column
    # keep it as an orphan (harmless — nothing populates or reads it).


def _add_column_if_missing(conn: sqlite3.Connection, table: str,
                            column: str, col_type: str) -> None:
    """Add `column` to `table` if it does not already exist. Idempotent."""
    existing = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()
        except Exception:
            pass  # Race condition on concurrent opens — harmless


def backup_db(db_path: Optional[str] = None) -> Optional[str]:
    """Copy the enrichment DB to backup/dbs/ before any destructive
    migration. Returns the backup path, or None if the DB doesn't exist."""
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(SCRIPT_DIR, "backup", "dbs")
    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, f"enrichment.db.{ts}")
    shutil.copy2(path, dest)
    return dest


def seed_card_universe(conn: sqlite3.Connection,
                       oracle_entries: list[tuple[str, str]]) -> int:
    """Seed (oracle_id, name) pairs into the card_universe table. Used by
    the Scryfall bulk ingester to ensure every oracle_id has at least
    one row so JOINs against enrichment never miss.

    Args:
        conn: enrichment.db connection
        oracle_entries: list of (oracle_id, name) tuples
    Returns:
        number of rows upserted
    """
    ts = datetime.utcnow().isoformat(timespec="seconds")
    rows = [(oid, name, ts) for (oid, name) in oracle_entries]
    conn.executemany(
        """INSERT INTO card_universe (oracle_id, name, last_seen_bulk)
           VALUES (?, ?, ?)
           ON CONFLICT(oracle_id) DO UPDATE SET
               name = excluded.name,
               last_seen_bulk = excluded.last_seen_bulk""",
        rows,
    )
    conn.commit()
    return len(rows)


def record_sync_attempt(conn: sqlite3.Connection, source: str,
                        success: bool, error: Optional[str] = None,
                        coverage_pct: Optional[float] = None,
                        version_hash: Optional[str] = None) -> None:
    """Record a refresh attempt in sync_metadata. Called by every
    EnrichmentSource.refresh() on completion (success or failure)."""
    # Collapse the prior SELECT-then-INSERT/UPDATE into one atomic
    # INSERT … ON CONFLICT DO UPDATE (sync_metadata.source is PRIMARY KEY).
    # Two statements are needed because the success path clears the error
    # and refreshes version_hash, while the failure path must NOT touch
    # last_success or version_hash on existing rows.
    ts = datetime.utcnow().isoformat(timespec="seconds")
    if success:
        conn.execute(
            """INSERT INTO sync_metadata
                   (source, last_success, last_attempt,
                    version_hash, error, coverage_pct)
               VALUES (?, ?, ?, ?, NULL, ?)
               ON CONFLICT(source) DO UPDATE SET
                   last_success = excluded.last_success,
                   last_attempt = excluded.last_attempt,
                   version_hash = COALESCE(excluded.version_hash,
                                           sync_metadata.version_hash),
                   error        = NULL,
                   coverage_pct = COALESCE(excluded.coverage_pct,
                                           sync_metadata.coverage_pct)""",
            (source, ts, ts, version_hash, coverage_pct),
        )
    else:
        conn.execute(
            """INSERT INTO sync_metadata
                   (source, last_success, last_attempt,
                    version_hash, error, coverage_pct)
               VALUES (?, NULL, ?, ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET
                   last_attempt = excluded.last_attempt,
                   error        = excluded.error,
                   coverage_pct = COALESCE(excluded.coverage_pct,
                                           sync_metadata.coverage_pct)""",
            (source, ts, version_hash, error, coverage_pct),
        )
    conn.commit()


def record_coverage(conn: sqlite3.Connection, source: str,
                    key_name: Optional[str],
                    expected: Optional[int], actual: Optional[int],
                    diff_json: Optional[str] = None) -> None:
    """Append a coverage_reports row. Append-only; older rows retained."""
    ts = datetime.utcnow().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO coverage_reports
           (source, run_at, key_name, expected, actual, diff_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (source, ts, key_name, expected, actual, diff_json),
    )
    conn.commit()


def get_sync_metadata(conn: sqlite3.Connection) -> list[dict]:
    """All source sync state as dicts. Used by /api/enrichment/sources."""
    rows = conn.execute(
        "SELECT * FROM sync_metadata ORDER BY source"
    ).fetchall()
    return [dict(r) for r in rows]


def rebuild_deck_usage(conn: sqlite3.Connection) -> int:
    """Rebuild the deck_usage overlay table from moxfield_decks and
    moxfield_wishlists.

    Uses a single atomic transaction: DELETE all existing rows, then
    INSERT the aggregated counts.  Never leaves a partial state.

    Deck counts are computed by parsing the raw_json of every cached deck
    and counting distinct oracle_ids (mainboard + commanders; no sideboard
    to match the import default).  Wishlist counts are per distinct
    (oracle_id) row in moxfield_wishlists regardless of username.

    Args:
        conn: open enrichment.db connection (must have deck_usage,
              moxfield_decks, and moxfield_wishlists tables).

    Returns:
        Number of oracle_id rows written into deck_usage.
    """
    import json as _json

    ts = datetime.utcnow().isoformat(timespec="seconds")

    # --- Aggregate deck counts from moxfield_decks raw_json ----------------
    deck_oracle_counts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT deck_id, raw_json FROM moxfield_decks"
    ).fetchall()
    for row in rows:
        raw = row["raw_json"] if hasattr(row, "keys") else row[1]
        if not raw:
            continue
        try:
            data = _json.loads(raw)
        except (ValueError, TypeError):
            continue
        seen: set[str] = set()
        for board_key in ("mainboard", "commanders"):
            board = data.get(board_key) or {}
            for _k, slot in board.items():
                card = slot.get("card") or {} if isinstance(slot, dict) else {}
                oid = card.get("oracle_id") or ""
                if oid:
                    seen.add(oid)
        for oid in seen:
            deck_oracle_counts[oid] = deck_oracle_counts.get(oid, 0) + 1

    # --- Aggregate wishlist counts from moxfield_wishlists ------------------
    wishlist_rows = conn.execute(
        "SELECT oracle_id, COUNT(DISTINCT username) AS cnt "
        "FROM moxfield_wishlists GROUP BY oracle_id"
    ).fetchall()
    wishlist_oracle_counts: dict[str, int] = {
        (r["oracle_id"] if hasattr(r, "keys") else r[0]):
        (r["cnt"] if hasattr(r, "keys") else r[1])
        for r in wishlist_rows
    }

    # --- Union the two sets of oracle_ids -----------------------------------
    all_oracle_ids = set(deck_oracle_counts) | set(wishlist_oracle_counts)
    insert_rows = [
        (
            oid,
            deck_oracle_counts.get(oid, 0),
            wishlist_oracle_counts.get(oid, 0),
            ts,
        )
        for oid in all_oracle_ids
    ]

    # --- Atomic replace -----------------------------------------------------
    with conn:
        conn.execute("DELETE FROM deck_usage")
        if insert_rows:
            conn.executemany(
                """INSERT INTO deck_usage
                       (oracle_id, deck_count, wishlist_count, updated_at)
                   VALUES (?, ?, ?, ?)""",
                insert_rows,
            )

    return len(insert_rows)
