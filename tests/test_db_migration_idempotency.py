"""
Migration idempotency tests for collection_db and enrichment_db.

Both modules ship with PRAGMA-style additive migrations: each new column
is wrapped in a `try: SELECT col FROM tbl LIMIT 1; except: ALTER TABLE`
(or, in enrichment_db's case, a `PRAGMA table_info` check via
`_add_column_if_missing`). These tests pin two contracts:

  1. **Idempotency** — calling get_connection() twice (or three times)
     on the same database file does not raise. New installs and
     in-place upgrades take the same code path; running it twice in
     succession should be a no-op the second time.

  2. **Forward migration from old schema** — if a database file exists
     that was created before a given column was added, get_connection
     adds the column rather than erroring. We simulate the
     "pre-migration" state by hand-building tables that match the
     pre-migration shape, then opening through get_connection() and
     asserting the new columns appear.

If a future change introduces a non-idempotent statement (e.g. an
unconditional ALTER, a CREATE TABLE without IF NOT EXISTS), the
idempotency tests fail on the second open. If a future change adds a
column but forgets the migration block, the forward-migration test
for that column fails.
"""

import os
import sqlite3
import sys
import tempfile
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collection_db
import enrichment_db


def _table_columns(conn, table):
    """Return the set of column names on `table`."""
    return {row[1] for row in conn.execute(
        f"PRAGMA table_info({table})").fetchall()}


# =========================================================================
# collection_db
# =========================================================================

class CollectionDBIdempotencyTests(unittest.TestCase):
    """Open the same DB through get_connection() multiple times and
    verify no errors and stable schema."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='cm_migidem_col_')
        self.db_path = os.path.join(self.tmpdir, "test.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_double_open_no_error(self):
        """get_connection on the same file twice — no exceptions."""
        conn1 = collection_db.get_connection(db_path=self.db_path)
        conn1.close()
        # Second open exercises every CREATE IF NOT EXISTS and every
        # `try: SELECT col; except: ALTER` block on a populated DB.
        conn2 = collection_db.get_connection(db_path=self.db_path)
        try:
            cols = _table_columns(conn2, "inventory")
            self.assertIn("box", cols)
            self.assertIn("foil_quantity", cols)
            self.assertIn("divider_id", cols)
        finally:
            conn2.close()

    def test_triple_open_no_error(self):
        """Belt-and-suspenders: three opens should also be clean."""
        for _ in range(3):
            conn = collection_db.get_connection(db_path=self.db_path)
            conn.close()
        conn = collection_db.get_connection(db_path=self.db_path)
        try:
            # Confirm every migrated column is still present after the
            # third pass and the schema is internally consistent.
            scan_cols = _table_columns(conn, "scan_history")
            for col in ("is_foil", "foil_confidence", "frame",
                        "border_color", "frame_effects"):
                self.assertIn(col, scan_cols,
                              f"scan_history missing migrated column {col!r}")
            session_cols = _table_columns(conn, "sessions")
            self.assertIn("config_text", session_cols)
        finally:
            conn.close()


class CollectionDBForwardMigrationTests(unittest.TestCase):
    """Hand-build a 'pre-migration' DB and verify get_connection adds
    the missing columns rather than crashing on the SELECT-then-ALTER
    path."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='cm_migfwd_col_')
        self.db_path = os.path.join(self.tmpdir, "test.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_pre_migration_db(self, *, with_box=False, with_foil=False,
                                with_divider=False, with_frame=False,
                                with_config_text=False):
        """Build a collection.db with the full pre-migration schema.

        Mirrors the CREATE TABLE statements in collection_db._create_tables
        but omits the columns added later via ALTER TABLE migrations.
        Indices the application creates are NOT pre-built here — the
        CREATE INDEX IF NOT EXISTS calls in _create_tables will add
        them on first connection, which is exactly the upgrade path
        we want to exercise.

        Each kwarg promotes a migration column to "already present"
        so partial-pre-migration states can also be simulated.
        """
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                sort_mode TEXT,
                config_name TEXT,
                bin_count INTEGER,
                total_scans INTEGER DEFAULT 0,
                recognized INTEGER DEFAULT 0,
                unrecognized INTEGER DEFAULT 0,
                notes TEXT
            );
            CREATE TABLE scan_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                scan_num INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                name TEXT,
                set_code TEXT,
                collector_number TEXT,
                oracle_id TEXT,
                illustration_id TEXT,
                colors TEXT,
                cmc REAL,
                type_line TEXT,
                rarity TEXT,
                price_usd REAL,
                bin INTEGER,
                method TEXT,
                hash_distance REAL,
                recognized INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE TABLE inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                set_code TEXT NOT NULL,
                collector_number TEXT NOT NULL DEFAULT '',
                oracle_id TEXT,
                illustration_id TEXT,
                colors TEXT,
                cmc REAL,
                type_line TEXT,
                rarity TEXT,
                price_usd REAL,
                quantity INTEGER NOT NULL DEFAULT 1,
                first_scanned TEXT,
                last_scanned TEXT
            );
        """)
        if with_box:
            conn.execute("ALTER TABLE inventory ADD COLUMN box TEXT")
        if with_foil:
            conn.execute(
                "ALTER TABLE scan_history ADD COLUMN is_foil "
                "INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "ALTER TABLE scan_history ADD COLUMN foil_confidence REAL")
            conn.execute(
                "ALTER TABLE inventory ADD COLUMN foil_quantity "
                "INTEGER NOT NULL DEFAULT 0")
        if with_divider:
            conn.execute("ALTER TABLE inventory ADD COLUMN divider_id INTEGER")
        if with_frame:
            conn.execute("ALTER TABLE scan_history ADD COLUMN frame TEXT")
            conn.execute("ALTER TABLE scan_history ADD COLUMN border_color TEXT")
            conn.execute("ALTER TABLE scan_history ADD COLUMN frame_effects TEXT")
        if with_config_text:
            conn.execute("ALTER TABLE sessions ADD COLUMN config_text TEXT")
        conn.commit()
        conn.close()

    def test_migration_adds_box_column(self):
        """An older DB without `inventory.box` gets the column."""
        self._build_pre_migration_db()
        conn = collection_db.get_connection(db_path=self.db_path)
        try:
            self.assertIn("box", _table_columns(conn, "inventory"))
        finally:
            conn.close()

    def test_migration_adds_foil_columns(self):
        """An older DB without foil columns gets all three added."""
        self._build_pre_migration_db()
        conn = collection_db.get_connection(db_path=self.db_path)
        try:
            scan_cols = _table_columns(conn, "scan_history")
            inv_cols = _table_columns(conn, "inventory")
            self.assertIn("is_foil", scan_cols)
            self.assertIn("foil_confidence", scan_cols)
            self.assertIn("foil_quantity", inv_cols)
        finally:
            conn.close()

    def test_migration_adds_divider_id(self):
        """An older DB without `inventory.divider_id` gets it."""
        self._build_pre_migration_db()
        conn = collection_db.get_connection(db_path=self.db_path)
        try:
            self.assertIn("divider_id", _table_columns(conn, "inventory"))
        finally:
            conn.close()

    def test_migration_adds_frame_columns(self):
        """An older DB without frame/border/effects gets all three added."""
        self._build_pre_migration_db()
        conn = collection_db.get_connection(db_path=self.db_path)
        try:
            cols = _table_columns(conn, "scan_history")
            self.assertIn("frame", cols)
            self.assertIn("border_color", cols)
            self.assertIn("frame_effects", cols)
        finally:
            conn.close()

    def test_migration_adds_config_text(self):
        """An older DB without `sessions.config_text` gets it. This is
        the Phase 4 power-loss-resume migration."""
        self._build_pre_migration_db()
        conn = collection_db.get_connection(db_path=self.db_path)
        try:
            self.assertIn("config_text", _table_columns(conn, "sessions"))
        finally:
            conn.close()

    def test_partial_pre_migration_dbs(self):
        """Mixed state: some migration columns already present, others
        missing. get_connection should add only the missing ones and
        leave the present ones alone (no duplicate-column error)."""
        # Some old DBs were partially patched by hand. Simulate one
        # that has `box` but not the foil columns or divider_id.
        self._build_pre_migration_db(with_box=True)
        conn = collection_db.get_connection(db_path=self.db_path)
        try:
            inv_cols = _table_columns(conn, "inventory")
            scan_cols = _table_columns(conn, "scan_history")
            # The previously-present column survived
            self.assertIn("box", inv_cols)
            # All the previously-missing columns are now present
            self.assertIn("foil_quantity", inv_cols)
            self.assertIn("divider_id", inv_cols)
            self.assertIn("is_foil", scan_cols)
        finally:
            conn.close()


# =========================================================================
# enrichment_db
# =========================================================================

class EnrichmentDBIdempotencyTests(unittest.TestCase):
    """Open the enrichment DB through get_connection() multiple times
    and verify no errors and stable schema."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='cm_migidem_enr_')
        self.db_path = os.path.join(self.tmpdir, "enrichment.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_double_open_no_error(self):
        conn1 = enrichment_db.get_connection(db_path=self.db_path)
        conn1.close()
        conn2 = enrichment_db.get_connection(db_path=self.db_path)
        try:
            cols = _table_columns(conn2, "tag_catalog")
            self.assertIn("last_updated", cols)
        finally:
            conn2.close()

    def test_triple_open_no_error(self):
        for _ in range(3):
            conn = enrichment_db.get_connection(db_path=self.db_path)
            conn.close()
        # Verify a representative cross-section of tables still exists
        # and has the expected columns after multiple passes.
        conn = enrichment_db.get_connection(db_path=self.db_path)
        try:
            for table in ("tags", "tag_catalog", "staples", "card_rankings",
                          "moxfield_wishlists", "moxfield_decks"):
                rows = conn.execute(
                    f"SELECT 1 FROM sqlite_master WHERE type='table' "
                    f"AND name='{table}'").fetchall()
                self.assertEqual(len(rows), 1, f"{table!r} missing after re-init")
        finally:
            conn.close()


class EnrichmentDBForwardMigrationTests(unittest.TestCase):
    """Verify _add_column_if_missing actually adds the column."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='cm_migfwd_enr_')
        self.db_path = os.path.join(self.tmpdir, "enrichment.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_migration_adds_last_updated_to_tag_catalog(self):
        """An older DB with tag_catalog but no `last_updated` column
        gets the column added by _migrate_existing_schema."""
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE tag_catalog (
                tag_name TEXT PRIMARY KEY,
                tag_type TEXT,
                description TEXT
            );
        """)
        conn.commit()
        conn.close()

        # Trigger migration through the public API
        conn = enrichment_db.get_connection(db_path=self.db_path)
        try:
            cols = _table_columns(conn, "tag_catalog")
            self.assertIn("last_updated", cols)
        finally:
            conn.close()

    def test_add_column_if_missing_is_no_op_when_present(self):
        """_add_column_if_missing should not raise when the column
        already exists."""
        # Fresh DB through get_connection (column already added on
        # first init). Re-running the helper should be safe.
        conn = enrichment_db.get_connection(db_path=self.db_path)
        try:
            # Call twice — second call must be a no-op.
            enrichment_db._add_column_if_missing(
                conn, "tag_catalog", "last_updated", "TEXT")
            enrichment_db._add_column_if_missing(
                conn, "tag_catalog", "last_updated", "TEXT")
            # Still only one column with that name
            cols = [row[1] for row in conn.execute(
                "PRAGMA table_info(tag_catalog)").fetchall()]
            self.assertEqual(cols.count("last_updated"), 1)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
