# collection_db.py
# ---------------------------------------------------------------------------
# SQLite-based collection database for tracking all scanned cards.
#
# Tables:
#   sessions      — one row per sorting session
#   scan_history  — append-only log of every scan ever performed
#   inventory     — cards in the collection with quantity
#
# Every recognized scan adds the card to inventory (quantity+1 if it
# already exists). The inventory is just "cards I own" — no location
# tracking since cards get re-sorted into different bins over time.
# ---------------------------------------------------------------------------

import os
import sqlite3
from datetime import datetime

from config import SCRIPT_DIR

DB_PATH = os.path.join(SCRIPT_DIR, "collection.db")


def get_connection(db_path=None):
    """Get a connection to the collection database, creating tables if needed."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _create_tables(conn)
    return conn


def _create_tables(conn):
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
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

        CREATE TABLE IF NOT EXISTS scan_history (
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
            is_foil INTEGER NOT NULL DEFAULT 0,
            foil_confidence REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS inventory (
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
            foil_quantity INTEGER NOT NULL DEFAULT 0,
            first_scanned TEXT,
            last_scanned TEXT,
            box TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_inventory_card
            ON inventory(name, set_code, collector_number);

        CREATE INDEX IF NOT EXISTS idx_inventory_oracle
            ON inventory(oracle_id);

        CREATE INDEX IF NOT EXISTS idx_scan_history_session
            ON scan_history(session_id);

        CREATE TABLE IF NOT EXISTS wishlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            set_code TEXT,
            max_price REAL,
            priority TEXT DEFAULT 'normal',
            notes TEXT,
            added_date TEXT,
            found INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS boxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            capacity INTEGER,
            created_at TEXT NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS dividers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            box_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            position INTEGER NOT NULL,
            capacity INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (box_id) REFERENCES boxes(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_dividers_box_pos
            ON dividers(box_id, position);

        CREATE TABLE IF NOT EXISTS detection_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL,
            variable TEXT NOT NULL,
            detected_value TEXT,
            confidence REAL,
            verdict TEXT,
            correction TEXT,
            reviewed_at TEXT,
            UNIQUE(scan_id, variable),
            FOREIGN KEY (scan_id) REFERENCES scan_history(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_detection_reviews_variable
            ON detection_reviews(variable, verdict, confidence);

        -- Moxfield wishlist source cache (Phase 4.21 priority bin).
        CREATE TABLE IF NOT EXISTS moxfield_wishlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL UNIQUE,
            username TEXT,
            display_name TEXT,
            last_synced TEXT,
            card_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS moxfield_wishlist_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wishlist_id INTEGER NOT NULL,
            oracle_id TEXT NOT NULL,
            name TEXT,
            set_code TEXT,
            image_uri TEXT,
            FOREIGN KEY (wishlist_id)
                REFERENCES moxfield_wishlists(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_mox_wishlist_card_wid
            ON moxfield_wishlist_cards(wishlist_id);
        CREATE INDEX IF NOT EXISTS idx_mox_wishlist_card_oid
            ON moxfield_wishlist_cards(oracle_id);
    """)
    # Migration: add box column if missing (existing databases)
    try:
        conn.execute("SELECT box FROM inventory LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE inventory ADD COLUMN box TEXT")
        conn.commit()

    # Migration: add foil tracking columns if missing (existing databases)
    try:
        conn.execute("SELECT is_foil FROM scan_history LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE scan_history ADD COLUMN is_foil INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "ALTER TABLE scan_history ADD COLUMN foil_confidence REAL"
        )
        conn.commit()
    try:
        conn.execute("SELECT foil_quantity FROM inventory LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE inventory ADD COLUMN foil_quantity INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()

    # Migration: add divider_id column if missing (Phase 2.12)
    try:
        conn.execute("SELECT divider_id FROM inventory LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE inventory ADD COLUMN divider_id INTEGER")
        conn.commit()

    # Migration: sync_manifests table (Phase 3.16 — Moxfield push)
    # Tracks last-uploaded state per oracle_id per target so diff-based sync
    # only pushes the delta.  Using a migration (not CREATE TABLE in the main
    # executescript) so existing databases get the table seamlessly.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_manifests (
            target        TEXT NOT NULL,
            oracle_id     TEXT NOT NULL,
            qty           INTEGER,
            foil_qty      INTEGER,
            condition     TEXT,
            last_uploaded_at TEXT,
            PRIMARY KEY (target, oracle_id)
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def start_session(conn, sort_mode=None, config_name=None, bin_count=None,
                  notes=None):
    """Create a new session record. Returns the session_id."""
    cursor = conn.execute(
        """INSERT INTO sessions (start_time, sort_mode, config_name,
                                bin_count, notes)
           VALUES (?, ?, ?, ?, ?)""",
        (datetime.now().isoformat(), sort_mode, config_name,
         bin_count, notes)
    )
    conn.commit()
    session_id = cursor.lastrowid
    print(f"[collection] Session #{session_id} started")
    return session_id


def end_session(conn, session_id, total_scans=0, recognized=0, unrecognized=0):
    """Update session with final stats."""
    conn.execute(
        """UPDATE sessions
           SET end_time=?, total_scans=?, recognized=?, unrecognized=?
           WHERE id=?""",
        (datetime.now().isoformat(), total_scans, recognized, unrecognized,
         session_id)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Recording scans
# ---------------------------------------------------------------------------

def record_scan(conn, session_id, scan_num,
                card_info=None, card_data=None, bin_num=None,
                method=None, hash_distance=None,
                is_foil=False, foil_confidence=None):
    """
    Record a scan to both scan_history and inventory.

    Every recognized card is added to inventory. If the card already
    exists (same name + set + collector_number), quantity is incremented.

    :param is_foil:         bool, whether the scanned card was detected as foil
    :param foil_confidence: float, raw confidence score from foil_detect
                            (None if detection was skipped or unavailable)
    """
    timestamp = datetime.now().isoformat()
    recognized = card_info is not None

    # Extract fields
    name = card_info.get('Name', '') if card_info else ''
    set_code = card_info.get('Set', '') if card_info else ''
    collector_number = ''
    oracle_id = None
    illustration_id = None
    colors = ''
    cmc = None
    type_line = ''
    rarity = ''
    price_usd = None

    if card_data:
        collector_number = card_data.get('collector_number', '')
        oracle_id = card_data.get('oracle_id')
        illustration_id = card_data.get('illustration_id')
        type_line = card_data.get('type_line', '')
        rarity = card_data.get('rarity', '')
        prices = card_data.get('prices', {})
        if prices:
            try:
                price_usd = float(prices.get('usd') or prices.get('usd_foil') or 0)
            except (ValueError, TypeError):
                price_usd = None

    if card_info:
        colors = ''.join(card_info.get('Colors', []))
        cmc = card_info.get('CMC')
        if not type_line:
            types = card_info.get('Types', [])
            type_line = ' '.join(types) if isinstance(types, list) else str(types)
        if not rarity:
            rarity = card_info.get('Rarity', '')
        if price_usd is None:
            try:
                p = card_info.get('Price', '')
                if p and p != 'N/A':
                    price_usd = float(str(p).replace('$', ''))
            except (ValueError, TypeError):
                pass

    is_foil_int = 1 if is_foil else 0

    # --- 1. Always append to scan_history ---
    conn.execute(
        """INSERT INTO scan_history
           (session_id, scan_num, timestamp, name, set_code, collector_number,
            oracle_id, illustration_id, colors, cmc, type_line, rarity,
            price_usd, bin, method, hash_distance, recognized,
            is_foil, foil_confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, scan_num, timestamp, name, set_code, collector_number,
         oracle_id, illustration_id, colors, cmc, type_line, rarity,
         price_usd, bin_num, method, hash_distance, int(recognized),
         is_foil_int, foil_confidence)
    )

    # --- 2. Update inventory (only for recognized cards) ---
    if recognized and name:
        existing = conn.execute(
            """SELECT id, quantity, foil_quantity FROM inventory
               WHERE name=? AND set_code=? AND collector_number=?
               LIMIT 1""",
            (name, set_code, collector_number)
        ).fetchone()

        foil_inc = 1 if is_foil else 0
        nonfoil_inc = 0 if is_foil else 1

        if existing:
            conn.execute(
                """UPDATE inventory
                   SET quantity=quantity+?,
                       foil_quantity=foil_quantity+?,
                       last_scanned=?,
                       price_usd=COALESCE(?, price_usd)
                   WHERE id=?""",
                (nonfoil_inc + foil_inc, foil_inc,
                 timestamp, price_usd, existing['id'])
            )
        else:
            conn.execute(
                """INSERT INTO inventory
                   (name, set_code, collector_number, oracle_id,
                    illustration_id, colors, cmc, type_line, rarity,
                    price_usd, quantity, foil_quantity,
                    first_scanned, last_scanned)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (name, set_code, collector_number, oracle_id,
                 illustration_id, colors, cmc, type_line, rarity,
                 price_usd, foil_inc, timestamp, timestamp)
            )

    conn.commit()


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------

def get_collection_stats(conn):
    """Get overall collection statistics."""
    row = conn.execute(
        """SELECT
             COUNT(*) as unique_cards,
             COALESCE(SUM(quantity), 0) as total_cards,
             COALESCE(SUM(quantity * COALESCE(price_usd, 0)), 0) as total_value
           FROM inventory"""
    ).fetchone()

    session_count = conn.execute(
        "SELECT COUNT(*) FROM sessions"
    ).fetchone()[0]

    return {
        "unique_cards": row['unique_cards'],
        "total_cards": row['total_cards'],
        "total_value": row['total_value'],
        "sessions": session_count,
    }


def get_inventory(conn, order_by="name", order_dir="ASC", limit=None):
    """Get the full inventory."""
    allowed_sort = {"name", "set_code", "price_usd", "quantity", "cmc",
                    "rarity", "colors", "last_scanned", "box", "type_line"}
    if order_by not in allowed_sort:
        order_by = "name"
    direction = "DESC" if order_dir.upper() == "DESC" else "ASC"

    query = f"SELECT * FROM inventory ORDER BY {order_by} {direction}"
    if limit:
        query += f" LIMIT {int(limit)}"

    rows = conn.execute(query).fetchall()
    return [dict(r) for r in rows]


def search_collection(conn, name=None, set_code=None, colors=None,
                      rarity=None, type_line=None, min_price=None,
                      max_price=None, box=None,
                      order_by="name", order_dir="ASC"):
    """Search the inventory with optional filters."""
    conditions = []
    params = []

    if name:
        conditions.append("name LIKE ?")
        params.append(f"%{name}%")
    if set_code:
        conditions.append("set_code=?")
        params.append(set_code)
    if colors:
        conditions.append("colors LIKE ?")
        params.append(f"%{colors}%")
    if rarity:
        conditions.append("rarity=?")
        params.append(rarity)
    if type_line:
        conditions.append("type_line LIKE ?")
        params.append(f"%{type_line}%")
    if min_price is not None:
        conditions.append("price_usd >= ?")
        params.append(min_price)
    if max_price is not None:
        conditions.append("price_usd <= ?")
        params.append(max_price)
    if box is not None:
        if box == '__unassigned__':
            conditions.append("box IS NULL")
        else:
            conditions.append("box=?")
            params.append(box)

    allowed_sort = {"name", "set_code", "price_usd", "quantity", "cmc",
                    "rarity", "colors", "last_scanned", "box", "type_line"}
    if order_by not in allowed_sort:
        order_by = "name"
    direction = "DESC" if order_dir.upper() == "DESC" else "ASC"

    where = " AND ".join(conditions) if conditions else "1=1"
    query = f"SELECT * FROM inventory WHERE {where} ORDER BY {order_by} {direction}"

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_duplicates(conn, min_quantity=2):
    """Find cards with multiple copies."""
    rows = conn.execute(
        """SELECT name, set_code, collector_number, quantity, price_usd
           FROM inventory WHERE quantity >= ? ORDER BY quantity DESC""",
        (min_quantity,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_session_history(conn):
    """Get all sessions."""
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY start_time DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_cull_candidates(conn, max_price=1.0, enr_db_path=None):
    """Return owned inventory rows that are cull candidates.

    A card qualifies if ALL of:
    - Has otag:vanilla or otag:french-vanilla in enrichment tags
    - Has no row in enrichment staples (any tier/source)
    - price_usd is NULL or < max_price

    Returns [] gracefully if enrichment.db is missing or tags are unpopulated.
    """
    import os
    import enrichment_db as _edb
    enr_path = enr_db_path or _edb.DB_PATH

    if not os.path.exists(enr_path):
        return []

    attached = False
    try:
        conn.execute("ATTACH DATABASE ? AS enr", (enr_path,))
        attached = True

        rows = conn.execute(
            """
            SELECT
                i.id,
                i.name,
                i.set_code,
                i.collector_number,
                i.oracle_id,
                i.type_line,
                i.rarity,
                i.colors,
                i.price_usd,
                i.quantity,
                GROUP_CONCAT(DISTINCT t.tag_name) AS matched_tags
            FROM inventory i
            JOIN enr.tags t
                ON t.oracle_id = i.oracle_id
               AND t.tag_name IN ('vanilla', 'french-vanilla')
            WHERE i.oracle_id NOT IN (
                SELECT DISTINCT oracle_id FROM enr.staples
            )
            AND (i.price_usd IS NULL OR i.price_usd < ?)
            GROUP BY i.id
            ORDER BY i.name ASC
            """,
            (max_price,),
        ).fetchall()

        result = []
        for row in rows:
            d = dict(row)
            tags = set((d.pop("matched_tags") or "").split(","))
            reasons = []
            if "vanilla" in tags:
                reasons.append("vanilla")
            if "french-vanilla" in tags:
                reasons.append("french-vanilla")
            reasons.append("no_staple")
            reasons.append("low_price")
            d["cull_reasons"] = reasons
            result.append(d)
        return result

    except Exception:
        return []
    finally:
        if attached:
            try:
                conn.execute("DETACH DATABASE enr")
            except Exception:
                pass


def get_scan_history(conn, session_id=None, limit=None):
    """Get scan history, optionally filtered by session."""
    query = "SELECT * FROM scan_history"
    params = []
    if session_id:
        query += " WHERE session_id=?"
        params.append(session_id)
    query += " ORDER BY timestamp DESC"
    if limit:
        query += f" LIMIT {int(limit)}"

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_unrecognized_scans(conn, session_id=None, page=1, per_page=20):
    """
    Get unrecognized scans with pagination.

    Returns dict with 'items', 'total', 'page', 'pages'.
    Each item includes the scan_history row plus the session start_time
    (needed to locate card crop images on disk).
    """
    where = "sh.recognized = 0"
    params = []
    if session_id:
        where += " AND sh.session_id = ?"
        params.append(session_id)

    count_row = conn.execute(
        f"SELECT COUNT(*) FROM scan_history sh WHERE {where}", params
    ).fetchone()
    total = count_row[0]
    pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page

    rows = conn.execute(
        f"""SELECT sh.*, s.start_time as session_start_time
            FROM scan_history sh
            JOIN sessions s ON sh.session_id = s.id
            WHERE {where}
            ORDER BY sh.id DESC
            LIMIT ? OFFSET ?""",
        params + [per_page, offset]
    ).fetchall()

    return {
        'items': [dict(r) for r in rows],
        'total': total,
        'page': page,
        'pages': pages,
    }


def resolve_unrecognized_scan(conn, scan_id, card_info, card_data=None):
    """
    Resolve an unrecognized scan by updating it with correct card details
    and adding the card to inventory.

    :param scan_id:   The scan_history.id to update
    :param card_info: Dict with Name, Set, Colors, CMC, Types, Rarity, Price
    :param card_data: Optional Scryfall card dict for extra fields
    :returns: True if updated, False if scan not found or already recognized
    """
    row = conn.execute(
        "SELECT * FROM scan_history WHERE id = ?", (scan_id,)
    ).fetchone()
    if row is None:
        return False
    if row['recognized']:
        return False  # Already resolved

    name = card_info.get('Name', '')
    set_code = card_info.get('Set', '')
    collector_number = ''
    oracle_id = None
    illustration_id = None
    colors = ''.join(card_info.get('Colors', []))
    cmc = card_info.get('CMC')
    type_line = ''
    rarity = card_info.get('Rarity', '')
    price_usd = None

    if card_data:
        collector_number = card_data.get('collector_number', '')
        oracle_id = card_data.get('oracle_id')
        illustration_id = card_data.get('illustration_id')
        type_line = card_data.get('type_line', '')
        rarity = card_data.get('rarity', rarity)
        prices = card_data.get('prices', {})
        if prices:
            try:
                price_usd = float(prices.get('usd') or prices.get('usd_foil') or 0)
            except (ValueError, TypeError):
                pass

    if not type_line:
        types = card_info.get('Types', [])
        type_line = ' '.join(types) if isinstance(types, list) else str(types)

    if price_usd is None:
        try:
            p = card_info.get('Price', '')
            if p and p != 'N/A':
                price_usd = float(str(p).replace('$', ''))
        except (ValueError, TypeError):
            pass

    timestamp = datetime.now().isoformat()

    # Update scan_history
    conn.execute(
        """UPDATE scan_history
           SET name=?, set_code=?, collector_number=?, oracle_id=?,
               illustration_id=?, colors=?, cmc=?, type_line=?, rarity=?,
               price_usd=?, method='manual_review', recognized=1
           WHERE id=?""",
        (name, set_code, collector_number, oracle_id, illustration_id,
         colors, cmc, type_line, rarity, price_usd, scan_id)
    )

    # Update session stats
    conn.execute(
        """UPDATE sessions
           SET recognized = recognized + 1,
               unrecognized = MAX(0, unrecognized - 1)
           WHERE id = ?""",
        (row['session_id'],)
    )

    # Add to inventory
    if name:
        existing = conn.execute(
            """SELECT id, quantity FROM inventory
               WHERE name=? AND set_code=? AND collector_number=?
               LIMIT 1""",
            (name, set_code, collector_number)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE inventory
                   SET quantity=quantity+1, last_scanned=?,
                       price_usd=COALESCE(?, price_usd)
                   WHERE id=?""",
                (timestamp, price_usd, existing['id'])
            )
        else:
            conn.execute(
                """INSERT INTO inventory
                   (name, set_code, collector_number, oracle_id,
                    illustration_id, colors, cmc, type_line, rarity,
                    price_usd, quantity, first_scanned, last_scanned)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (name, set_code, collector_number, oracle_id,
                 illustration_id, colors, cmc, type_line, rarity,
                 price_usd, timestamp, timestamp)
            )

    conn.commit()
    print(f"[collection] Resolved scan #{scan_id}: {name} ({set_code})")
    return True


def export_inventory_csv(conn, filepath):
    """Export the full inventory to a CSV file."""
    import csv
    rows = get_inventory(conn, order_by="name")
    if not rows:
        print("[collection] No cards in inventory to export.")
        return

    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"[collection] Exported {len(rows)} cards to {filepath}")


def import_inventory_csv(conn, csv_text):
    """
    Import cards from CSV text into inventory.

    Expects columns: name, set_code, collector_number, and optionally
    oracle_id, illustration_id, colors, cmc, type_line, rarity,
    price_usd, quantity.

    Cards with matching (name, set_code, collector_number) get their
    quantity incremented; new cards are inserted.

    Returns (imported, updated, skipped) counts.
    """
    import csv
    import io

    reader = csv.DictReader(io.StringIO(csv_text))
    imported = 0
    updated = 0
    skipped = 0
    now = datetime.now().isoformat()

    for row in reader:
        name = row.get('name', '').strip()
        set_code = row.get('set_code', '').strip()
        if not name or not set_code:
            skipped += 1
            continue

        collector_number = row.get('collector_number', '').strip()
        quantity = int(row.get('quantity', 1) or 1)

        existing = conn.execute(
            """SELECT id, quantity FROM inventory
               WHERE name=? AND set_code=? AND collector_number=?
               LIMIT 1""",
            (name, set_code, collector_number)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE inventory SET quantity=quantity+?, last_scanned=?
                   WHERE id=?""",
                (quantity, now, existing['id'])
            )
            updated += 1
        else:
            def _f(key):
                val = row.get(key, '')
                return val if val else None

            try:
                price = float(row.get('price_usd', 0) or 0)
            except (ValueError, TypeError):
                price = None
            try:
                cmc = float(row.get('cmc', 0) or 0)
            except (ValueError, TypeError):
                cmc = None

            conn.execute(
                """INSERT INTO inventory
                   (name, set_code, collector_number, oracle_id,
                    illustration_id, colors, cmc, type_line, rarity,
                    price_usd, quantity, first_scanned, last_scanned)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, set_code, collector_number, _f('oracle_id'),
                 _f('illustration_id'), _f('colors'), cmc,
                 _f('type_line'), _f('rarity'), price, quantity, now, now)
            )
            imported += 1

    conn.commit()
    print(f"[collection] CSV import: {imported} new, {updated} updated, "
          f"{skipped} skipped")
    return imported, updated, skipped


def generate_test_collection(conn, count=50):
    """
    Generate a test collection by pulling random cards from the Scryfall
    bulk data. Returns the number of cards added.
    """
    import random
    try:
        from cards import CARDS_DATA
    except ImportError:
        return 0

    if not CARDS_DATA:
        return 0

    # Filter to paper cards with English names
    eligible = [c for c in CARDS_DATA
                if c.get('lang') == 'en'
                and 'paper' in c.get('games', [])
                and c.get('name')]

    if not eligible:
        return 0

    sample = random.sample(eligible, min(count, len(eligible)))
    now = datetime.now().isoformat()
    added = 0

    for card in sample:
        name = card.get('name', '')
        set_code = card.get('set', '')
        collector_number = card.get('collector_number', '')
        quantity = random.choices([1, 2, 3, 4], weights=[50, 30, 15, 5])[0]

        prices = card.get('prices', {})
        try:
            price_usd = float(prices.get('usd') or prices.get('usd_foil') or 0)
        except (ValueError, TypeError):
            price_usd = None

        colors = ''.join(card.get('colors', []))
        cmc = card.get('cmc')
        type_line = card.get('type_line', '')
        rarity = card.get('rarity', '')

        conn.execute(
            """INSERT INTO inventory
               (name, set_code, collector_number, oracle_id,
                illustration_id, colors, cmc, type_line, rarity,
                price_usd, quantity, first_scanned, last_scanned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, set_code, collector_number, card.get('oracle_id'),
             card.get('illustration_id'), colors, cmc, type_line, rarity,
             price_usd, quantity, now, now)
        )
        added += 1

    conn.commit()
    print(f"[collection] Generated test collection: {added} cards")
    return added


# ---------------------------------------------------------------------------
# Box management
# ---------------------------------------------------------------------------

def get_boxes(conn):
    """Get list of all unique box names in use."""
    rows = conn.execute(
        "SELECT DISTINCT box FROM inventory WHERE box IS NOT NULL ORDER BY box"
    ).fetchall()
    return [r['box'] for r in rows]


def get_box_summary(conn):
    """Get card count and total value per box."""
    rows = conn.execute(
        """SELECT
             COALESCE(box, '__unassigned__') as box_name,
             COUNT(*) as unique_cards,
             COALESCE(SUM(quantity), 0) as total_cards,
             COALESCE(SUM(quantity * COALESCE(price_usd, 0)), 0) as total_value
           FROM inventory
           GROUP BY COALESCE(box, '__unassigned__')
           ORDER BY box_name"""
    ).fetchall()
    return [dict(r) for r in rows]


def assign_box(conn, item_id, box_name, move_quantity=None):
    """
    Assign a box to an inventory item.

    If move_quantity is specified and less than the item's total quantity,
    splits the row: the original keeps (quantity - move_quantity) with its
    current box, and a new row is created with move_quantity in the new box.

    If move_quantity is None or equals total quantity, just updates the box.
    """
    row = conn.execute(
        "SELECT * FROM inventory WHERE id=?", (item_id,)
    ).fetchone()
    if not row:
        return False

    current_qty = row['quantity']
    box = box_name.strip() if box_name else None

    if move_quantity is not None and 0 < move_quantity < current_qty:
        # Split: reduce original, create new row with the new box
        conn.execute(
            "UPDATE inventory SET quantity=? WHERE id=?",
            (current_qty - move_quantity, item_id)
        )
        conn.execute(
            """INSERT INTO inventory
               (name, set_code, collector_number, oracle_id,
                illustration_id, colors, cmc, type_line, rarity,
                price_usd, quantity, first_scanned, last_scanned, box)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row['name'], row['set_code'], row['collector_number'],
             row['oracle_id'], row['illustration_id'], row['colors'],
             row['cmc'], row['type_line'], row['rarity'], row['price_usd'],
             move_quantity, row['first_scanned'], row['last_scanned'], box)
        )
    else:
        # Update box on the whole row
        conn.execute(
            "UPDATE inventory SET box=? WHERE id=?", (box, item_id)
        )

    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Boxes & dividers (Phase 2.12 — first-class storage tables)
# ---------------------------------------------------------------------------
#
# The legacy `inventory.box` TEXT column still exists and holds a free-form
# box name for backward-compatibility with existing UI code. Phase 2.12
# introduces the first-class `boxes` and `dividers` tables so a row in
# inventory can be pinned to a specific divider (which in turn belongs to a
# box). The new `inventory.divider_id` column is nullable: rows without a
# divider fall back to the legacy `box` TEXT grouping, and rows with
# neither are reported as "Unassigned" by the locator.

def add_box(conn, name, capacity=None, notes=None):
    """Create a new box. Returns the new box id.

    Raises sqlite3.IntegrityError if the name already exists.
    """
    now = datetime.now().isoformat()
    cursor = conn.execute(
        """INSERT INTO boxes (name, capacity, created_at, notes)
           VALUES (?, ?, ?, ?)""",
        (name, capacity, now, notes)
    )
    conn.commit()
    return cursor.lastrowid


def update_box(conn, box_id, name=None, capacity=None, notes=None):
    """Update fields on a box. Only provided (non-None) fields change.

    Returns True if the row existed, False otherwise.
    """
    row = conn.execute("SELECT id FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not row:
        return False

    fields = []
    params = []
    if name is not None:
        fields.append("name=?")
        params.append(name)
    if capacity is not None:
        fields.append("capacity=?")
        params.append(capacity)
    if notes is not None:
        fields.append("notes=?")
        params.append(notes)

    if not fields:
        return True
    params.append(box_id)
    conn.execute(f"UPDATE boxes SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    return True


def delete_box(conn, box_id):
    """Delete a box and cascade-delete its dividers.

    Does NOT delete inventory rows; their `divider_id` is set back to NULL
    via the foreign-key cascade path (sqlite honors our explicit NULL).
    Returns True if the box existed.
    """
    row = conn.execute("SELECT id FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not row:
        return False
    # Null out inventory references pointing at dividers in this box.
    conn.execute(
        """UPDATE inventory SET divider_id = NULL
           WHERE divider_id IN (SELECT id FROM dividers WHERE box_id=?)""",
        (box_id,)
    )
    conn.execute("DELETE FROM dividers WHERE box_id=?", (box_id,))
    conn.execute("DELETE FROM boxes WHERE id=?", (box_id,))
    conn.commit()
    return True


def list_boxes(conn):
    """Return all boxes with divider count + inventory card count.

    Each row is a dict: id, name, capacity, created_at, notes,
    divider_count, card_count.
    """
    rows = conn.execute(
        """SELECT b.*,
                  (SELECT COUNT(*) FROM dividers d WHERE d.box_id = b.id)
                      AS divider_count,
                  (SELECT COALESCE(SUM(i.quantity), 0)
                     FROM inventory i
                     JOIN dividers d2 ON i.divider_id = d2.id
                    WHERE d2.box_id = b.id) AS card_count
             FROM boxes b
            ORDER BY b.name"""
    ).fetchall()
    return [dict(r) for r in rows]


def add_divider(conn, box_id, label, position, capacity=None):
    """Create a new divider inside a box. Returns the new divider id.

    Raises ValueError if the box does not exist.
    """
    box_row = conn.execute(
        "SELECT id FROM boxes WHERE id=?", (box_id,)
    ).fetchone()
    if not box_row:
        raise ValueError(f"box_id {box_id} does not exist")
    now = datetime.now().isoformat()
    cursor = conn.execute(
        """INSERT INTO dividers (box_id, label, position, capacity, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (box_id, label, int(position), capacity, now)
    )
    conn.commit()
    return cursor.lastrowid


def update_divider(conn, divider_id, label=None, position=None, capacity=None):
    """Update fields on a divider. Only provided fields change."""
    row = conn.execute(
        "SELECT id FROM dividers WHERE id=?", (divider_id,)
    ).fetchone()
    if not row:
        return False

    fields = []
    params = []
    if label is not None:
        fields.append("label=?")
        params.append(label)
    if position is not None:
        fields.append("position=?")
        params.append(int(position))
    if capacity is not None:
        fields.append("capacity=?")
        params.append(capacity)

    if not fields:
        return True
    params.append(divider_id)
    conn.execute(f"UPDATE dividers SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    return True


def delete_divider(conn, divider_id):
    """Delete a divider; inventory rows pointing at it are unassigned."""
    row = conn.execute(
        "SELECT id FROM dividers WHERE id=?", (divider_id,)
    ).fetchone()
    if not row:
        return False
    conn.execute(
        "UPDATE inventory SET divider_id = NULL WHERE divider_id = ?",
        (divider_id,)
    )
    conn.execute("DELETE FROM dividers WHERE id = ?", (divider_id,))
    conn.commit()
    return True


def list_dividers(conn, box_id=None):
    """Return dividers, optionally scoped to a single box, ordered by
    (box_id, position).

    Each row is a dict plus a `box_name` field and a `card_count`.
    """
    where = "WHERE d.box_id = ?" if box_id is not None else ""
    params = (box_id,) if box_id is not None else ()
    rows = conn.execute(
        f"""SELECT d.*,
                   b.name AS box_name,
                   (SELECT COALESCE(SUM(i.quantity), 0)
                      FROM inventory i
                     WHERE i.divider_id = d.id) AS card_count
              FROM dividers d
              JOIN boxes b ON d.box_id = b.id
              {where}
             ORDER BY d.box_id, d.position""",
        params
    ).fetchall()
    return [dict(r) for r in rows]


def assign_inventory_divider(conn, item_id, divider_id):
    """Set or clear the divider_id for an inventory row.

    Pass divider_id=None to unassign. Returns True if the row existed.
    """
    row = conn.execute(
        "SELECT id FROM inventory WHERE id=?", (item_id,)
    ).fetchone()
    if not row:
        return False
    if divider_id is not None:
        div = conn.execute(
            "SELECT id FROM dividers WHERE id=?", (divider_id,)
        ).fetchone()
        if not div:
            raise ValueError(f"divider_id {divider_id} does not exist")
    conn.execute(
        "UPDATE inventory SET divider_id=? WHERE id=?",
        (divider_id, item_id)
    )
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Physical locator (Phase 2.11)
# ---------------------------------------------------------------------------

UNASSIGNED_BOX_NAME = "Unassigned"


def _inventory_row_to_card_data(row):
    """Project an inventory row into the Scryfall-shaped dict the
    query_parser expects. Used as a fallback when the full Scryfall card
    dict isn't available via card_lookup.
    """
    colors = [c for c in (row.get('colors') or '')]
    prices = {}
    if row.get('price_usd') is not None:
        prices['usd'] = str(row['price_usd'])
    return {
        'name': row.get('name') or '',
        'set': (row.get('set_code') or '').lower(),
        'collector_number': row.get('collector_number') or '',
        'oracle_id': row.get('oracle_id'),
        'illustration_id': row.get('illustration_id'),
        'colors': colors,
        'color_identity': colors,
        'cmc': row.get('cmc'),
        'type_line': row.get('type_line') or '',
        'rarity': (row.get('rarity') or '').lower(),
        'prices': prices,
        'oracle_text': '',
        'keywords': [],
        'legalities': {},
        'produced_mana': [],
        'set_type': '',
    }


def _resolve_card_data(row):
    """Try to look up a full Scryfall card dict for an inventory row,
    falling back to the projected minimal dict on failure.
    """
    # Prefer the loaded bulk data via card_lookup when available.
    try:
        import card_lookup
        set_code = row.get('set_code') or ''
        collector_number = row.get('collector_number') or ''
        full = card_lookup.lookup_by_set_collector(set_code, collector_number)
        if full:
            return full
    except Exception:
        pass
    return _inventory_row_to_card_data(row)


def locate_cards_by_query(conn, query_string, otag_cache=None):
    """Find owned cards matching a Scryfall-style query and aggregate the
    results by physical (box, divider) location.

    Returns a list of dicts:
        [{
            "box_name": str,
            "divider_label": str | None,
            "divider_id": int | None,
            "box_id": int | None,
            "count": int,   # total copies in this bucket (sum of quantity)
            "unique_cards": int,
            "oracle_ids": [str, ...],
        }, ...]

    Sorted by (box_name, divider position ASC with NULLs last).

    Cards with no divider_id fall under the legacy inventory.box TEXT
    grouping (box_name == row['box'] or "Unassigned" if both are NULL).
    """
    from query_parser import parse_query, evaluate_query

    query_string = (query_string or '').strip()
    if not query_string:
        return []

    ast = parse_query(query_string)

    rows = conn.execute(
        """SELECT i.*,
                  d.label  AS divider_label,
                  d.position AS divider_position,
                  d.box_id AS divider_box_id,
                  b.name   AS divider_box_name
             FROM inventory i
        LEFT JOIN dividers d ON i.divider_id = d.id
        LEFT JOIN boxes    b ON d.box_id = b.id"""
    ).fetchall()

    # Bucket by (box_name, divider_id) — fallback: (inventory.box, None)
    # -> further fallback: (UNASSIGNED_BOX_NAME, None).
    buckets = {}
    for r in rows:
        row = dict(r)
        card_data = _resolve_card_data(row)
        try:
            if not evaluate_query(ast, card_data, otag_cache):
                continue
        except Exception:
            continue

        if row.get('divider_id') is not None:
            box_name = row.get('divider_box_name') or UNASSIGNED_BOX_NAME
            box_id = row.get('divider_box_id')
            divider_id = row.get('divider_id')
            divider_label = row.get('divider_label')
            divider_position = row.get('divider_position')
        else:
            legacy_box = row.get('box')
            box_name = legacy_box if legacy_box else UNASSIGNED_BOX_NAME
            box_id = None
            divider_id = None
            divider_label = None
            divider_position = None

        key = (box_name, divider_id)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                'box_name': box_name,
                'box_id': box_id,
                'divider_id': divider_id,
                'divider_label': divider_label,
                'divider_position': divider_position,
                'count': 0,
                'unique_cards': 0,
                'oracle_ids': [],
                '_seen_oracle': set(),
            }
            buckets[key] = bucket

        qty = int(row.get('quantity') or 1)
        bucket['count'] += qty
        bucket['unique_cards'] += 1
        oid = row.get('oracle_id')
        if oid and oid not in bucket['_seen_oracle']:
            bucket['_seen_oracle'].add(oid)
            bucket['oracle_ids'].append(oid)

    def sort_key(b):
        # NULL dividers sort after real positions within the same box.
        pos = b['divider_position']
        return (
            b['box_name'] == UNASSIGNED_BOX_NAME,
            b['box_name'].lower(),
            0 if pos is not None else 1,
            pos if pos is not None else 0,
        )

    result = []
    for b in sorted(buckets.values(), key=sort_key):
        b.pop('_seen_oracle', None)
        # Drop divider_position from the public payload; it's an internal
        # sort key. Keep divider_id + label which are what callers want.
        b.pop('divider_position', None)
        result.append(b)
    return result


# ---------------------------------------------------------------------------
# Delete / reset operations
# ---------------------------------------------------------------------------

def increment_inventory_item(conn, item_id, quantity=1):
    """Add copies to an existing inventory item."""
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE inventory SET quantity = quantity + ?, last_scanned = ? WHERE id = ?",
        (quantity, now, item_id)
    )
    conn.commit()


def add_inventory_item(conn, name, set_code, collector_number='',
                       colors=None, type_line=None, rarity=None,
                       price_usd=None, quantity=1, box=None):
    """
    Manually add a card to inventory.

    If a matching (name, set_code, collector_number) already exists,
    increments its quantity instead.
    """
    now = datetime.now().isoformat()
    existing = conn.execute(
        """SELECT id, quantity FROM inventory
           WHERE name=? AND set_code=? AND collector_number=?
           LIMIT 1""",
        (name, set_code, collector_number)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE inventory SET quantity = quantity + ?, last_scanned = ? WHERE id = ?",
            (quantity, now, existing['id'])
        )
        conn.commit()
        return existing['id']

    conn.execute(
        """INSERT INTO inventory
           (name, set_code, collector_number, colors, type_line, rarity,
            price_usd, quantity, first_scanned, last_scanned, box)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, set_code, collector_number, colors, type_line, rarity,
         price_usd, quantity, now, now, box)
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def delete_inventory_item(conn, item_id, quantity=None):
    """
    Remove cards from inventory.

    If quantity is None or >= the item's current quantity, deletes the row.
    Otherwise decrements quantity by the specified amount.
    """
    if quantity is not None:
        row = conn.execute(
            "SELECT quantity FROM inventory WHERE id = ?", (item_id,)
        ).fetchone()
        if row and quantity < row['quantity']:
            conn.execute(
                "UPDATE inventory SET quantity = quantity - ? WHERE id = ?",
                (quantity, item_id)
            )
            conn.commit()
            return
    conn.execute("DELETE FROM inventory WHERE id = ?", (item_id,))
    conn.commit()


def delete_session(conn, session_id):
    """Delete a session and its scan history."""
    conn.execute("DELETE FROM scan_history WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


def reset_collection(conn):
    """Delete all data from all tables."""
    conn.executescript("""
        DELETE FROM scan_history;
        DELETE FROM sessions;
        DELETE FROM inventory;
        DELETE FROM dividers;
        DELETE FROM boxes;
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Wishlist
# ---------------------------------------------------------------------------

def get_wishlist(conn):
    """Get all wishlist items."""
    rows = conn.execute(
        "SELECT * FROM wishlist ORDER BY found ASC, priority DESC, name"
    ).fetchall()
    return [dict(r) for r in rows]


def add_wishlist_item(conn, name, set_code=None, max_price=None,
                      priority='normal', notes=None):
    """Add a card to the wishlist."""
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO wishlist (name, set_code, max_price, priority, notes,
                                added_date, found)
           VALUES (?, ?, ?, ?, ?, ?, 0)""",
        (name, set_code, max_price, priority, notes, now)
    )
    conn.commit()


def delete_wishlist_item(conn, item_id):
    """Remove an item from the wishlist."""
    conn.execute("DELETE FROM wishlist WHERE id = ?", (item_id,))
    conn.commit()


def mark_wishlist_found(conn, item_id):
    """Mark a wishlist item as found."""
    conn.execute("UPDATE wishlist SET found = 1 WHERE id = ?", (item_id,))
    conn.commit()


def check_wishlist_match(conn, card_name):
    """Check if a scanned card name matches any wishlist item. Returns matches."""
    rows = conn.execute(
        "SELECT * FROM wishlist WHERE found = 0 AND ? LIKE '%' || name || '%'",
        (card_name,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Moxfield wishlist cache (Phase 4.21 priority bin)
# ---------------------------------------------------------------------------

_BASIC_LAND_NAMES = {
    "plains", "island", "swamp", "mountain", "forest",
    "wastes", "snow-covered plains", "snow-covered island",
    "snow-covered swamp", "snow-covered mountain", "snow-covered forest",
}


def list_moxfield_wishlists(conn):
    rows = conn.execute(
        "SELECT * FROM moxfield_wishlists ORDER BY source_key"
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_moxfield_wishlist(conn, source_key, cards,
                             username=None, display_name=None):
    filtered = [
        c for c in cards
        if c.get('oracle_id')
        and (c.get('name') or '').strip().lower() not in _BASIC_LAND_NAMES
    ]
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO moxfield_wishlists
               (source_key, username, display_name, last_synced, card_count)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source_key) DO UPDATE SET
               username=excluded.username,
               display_name=excluded.display_name,
               last_synced=excluded.last_synced,
               card_count=excluded.card_count""",
        (source_key, username, display_name, now, len(filtered))
    )
    row = conn.execute(
        "SELECT id FROM moxfield_wishlists WHERE source_key=?",
        (source_key,)
    ).fetchone()
    wid = row['id']
    conn.execute(
        "DELETE FROM moxfield_wishlist_cards WHERE wishlist_id=?", (wid,)
    )
    conn.executemany(
        """INSERT INTO moxfield_wishlist_cards
               (wishlist_id, oracle_id, name, set_code, image_uri)
           VALUES (?, ?, ?, ?, ?)""",
        [(wid, c['oracle_id'], c.get('name'),
          c.get('set_code'), c.get('image_uri')) for c in filtered]
    )
    conn.commit()
    return wid


def get_moxfield_wishlist_cards(conn, source_key):
    rows = conn.execute(
        """SELECT c.oracle_id, c.name, c.set_code, c.image_uri
           FROM moxfield_wishlist_cards c
           JOIN moxfield_wishlists w ON c.wishlist_id = w.id
           WHERE w.source_key = ?""",
        (source_key,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_moxfield_wishlist_oracle_ids(conn, source_key):
    return {r['oracle_id']
            for r in get_moxfield_wishlist_cards(conn, source_key)}


def get_moxfield_wishlist_card(conn, source_key, oracle_id):
    row = conn.execute(
        """SELECT c.oracle_id, c.name, c.set_code, c.image_uri
           FROM moxfield_wishlist_cards c
           JOIN moxfield_wishlists w ON c.wishlist_id = w.id
           WHERE w.source_key = ? AND c.oracle_id = ?""",
        (source_key, oracle_id)
    ).fetchone()
    return dict(row) if row else None


def delete_moxfield_wishlist(conn, source_key):
    conn.execute(
        "DELETE FROM moxfield_wishlists WHERE source_key=?", (source_key,)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Per-attribute detection review queues
# ---------------------------------------------------------------------------

# Variables the review system knows about. Keeping this list centralized so
# the API layer, tests, and UI can validate without hard-coding strings.
DETECTION_VARIABLES = ('foil', 'border', 'set_symbol')

VALID_VERDICTS = ('correct', 'wrong', 'skip')


def upsert_detection_review(conn, scan_id, variable,
                            detected_value=None, confidence=None):
    """
    Idempotently insert (or refresh the detected value / confidence of) a
    review row for a (scan, variable) pair. Never overwrites an existing
    verdict — once a human has reviewed, the row is sticky.

    Returns the detection_reviews.id.
    """
    if variable not in DETECTION_VARIABLES:
        raise ValueError(f"unknown variable: {variable!r}")

    existing = conn.execute(
        "SELECT id, verdict FROM detection_reviews "
        "WHERE scan_id=? AND variable=?",
        (scan_id, variable)
    ).fetchone()
    if existing:
        # Refresh detected_value / confidence only when no verdict yet.
        if existing['verdict'] is None:
            conn.execute(
                """UPDATE detection_reviews
                   SET detected_value=?, confidence=?
                   WHERE id=?""",
                (detected_value, confidence, existing['id'])
            )
            conn.commit()
        return existing['id']

    cur = conn.execute(
        """INSERT INTO detection_reviews
           (scan_id, variable, detected_value, confidence)
           VALUES (?, ?, ?, ?)""",
        (scan_id, variable, detected_value, confidence)
    )
    conn.commit()
    return cur.lastrowid


def list_detection_reviews(conn, variable, include_reviewed=False, limit=200):
    """
    Return review items for a given variable, ordered ASCENDING by confidence
    so uncertain cases surface first. NULL confidences sort FIRST (most
    important to review — the detector didn't know).

    Joins scan_history + sessions so the UI has the data it needs to render
    the crop image and card context.
    """
    if variable not in DETECTION_VARIABLES:
        raise ValueError(f"unknown variable: {variable!r}")

    where = ["dr.variable = ?"]
    params = [variable]
    if not include_reviewed:
        where.append("dr.verdict IS NULL")

    sql = f"""
        SELECT dr.id, dr.scan_id, dr.variable, dr.detected_value,
               dr.confidence, dr.verdict, dr.correction, dr.reviewed_at,
               sh.scan_num, sh.name, sh.set_code, sh.collector_number,
               sh.session_id, s.start_time as session_start_time
        FROM detection_reviews dr
        JOIN scan_history sh ON dr.scan_id = sh.id
        LEFT JOIN sessions s ON sh.session_id = s.id
        WHERE {' AND '.join(where)}
        ORDER BY
          CASE WHEN dr.confidence IS NULL THEN 0 ELSE 1 END ASC,
          dr.confidence ASC,
          dr.id ASC
        LIMIT ?
    """
    rows = conn.execute(sql, params + [int(limit)]).fetchall()
    return [dict(r) for r in rows]


def set_detection_verdict(conn, review_id, verdict, correction=None):
    """
    Mark a review row with a verdict. Idempotent on the same verdict (just
    refreshes reviewed_at). Returns True on success, False if row missing.
    """
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")

    row = conn.execute(
        "SELECT id FROM detection_reviews WHERE id=?",
        (review_id,)
    ).fetchone()
    if not row:
        return False

    now = datetime.now().isoformat()
    conn.execute(
        """UPDATE detection_reviews
           SET verdict=?, correction=?, reviewed_at=?
           WHERE id=?""",
        (verdict, correction, now, review_id)
    )
    conn.commit()
    return True


def seed_detection_reviews_from_scans(conn, variables=None,
                                       only_recognized=True, limit=None):
    """
    Walk scan_history and ensure there is a detection_reviews row for every
    (scan, variable). Detectors that don't emit per-scan confidence today
    produce rows with confidence=NULL — those surface first in the queue.

    Detected values are pulled from whatever the DB already knows:
      - foil:       unknown (NULL) — no detector yet
      - border:     unknown (NULL) — detect_border_type() isn't persisted
      - set_symbol: set_code from scan_history (best available proxy)

    Intended to be called occasionally by the UI / a maintenance job, not on
    every scan. Idempotent: re-running won't duplicate rows or stomp verdicts.

    Returns the number of rows inserted (new reviews).
    """
    if variables is None:
        variables = DETECTION_VARIABLES
    for v in variables:
        if v not in DETECTION_VARIABLES:
            raise ValueError(f"unknown variable: {v!r}")

    where = []
    params = []
    if only_recognized:
        where.append("recognized = 1")
    sql = "SELECT id, set_code FROM scan_history"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    scans = conn.execute(sql, params).fetchall()

    inserted = 0
    for scan in scans:
        scan_id = scan['id']
        set_code = scan['set_code'] or None
        for variable in variables:
            detected = None
            if variable == 'set_symbol':
                # The best proxy for "detected set symbol" we currently have
                # is the set code that identification landed on. It's not
                # the symbol detector's own output — but it's what the user
                # would verify as right/wrong until a real detector lands.
                detected = set_code
            # foil + border: no detector output stored → leave NULL so these
            # cases bubble to the top of the queue.
            existing = conn.execute(
                "SELECT id FROM detection_reviews "
                "WHERE scan_id=? AND variable=?",
                (scan_id, variable)
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO detection_reviews
                   (scan_id, variable, detected_value, confidence)
                   VALUES (?, ?, ?, NULL)""",
                (scan_id, variable, detected)
            )
            inserted += 1
    conn.commit()
    return inserted


def get_detection_review_counts(conn):
    """Return {variable: {'pending': n, 'reviewed': n}} for the dashboard."""
    out = {v: {'pending': 0, 'reviewed': 0} for v in DETECTION_VARIABLES}
    rows = conn.execute(
        """SELECT variable,
                  SUM(CASE WHEN verdict IS NULL THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN verdict IS NOT NULL THEN 1 ELSE 0 END) AS reviewed
             FROM detection_reviews
             GROUP BY variable"""
    ).fetchall()
    for r in rows:
        v = r['variable']
        if v in out:
            out[v] = {
                'pending': int(r['pending'] or 0),
                'reviewed': int(r['reviewed'] or 0),
            }
    return out


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def print_collection_stats(conn):
    """Print collection overview to console."""
    stats = get_collection_stats(conn)
    print(f"\n{'═' * 50}")
    print(f"  Collection Overview")
    print(f"{'═' * 50}")
    print(f"  Unique cards:  {stats['unique_cards']}")
    print(f"  Total cards:   {stats['total_cards']}")
    print(f"  Total value:   ${stats['total_value']:.2f}")
    print(f"  Sessions:      {stats['sessions']}")
    print(f"{'═' * 50}\n")


def print_inventory_summary(conn, limit=20):
    """Print a summary of the inventory."""
    rows = get_inventory(conn, order_by="name", limit=limit)
    total = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]

    print(f"\n{'─' * 70}")
    print(f"  Inventory ({total} unique cards)")
    print(f"{'─' * 70}")
    print(f"  {'Name':<30} {'Set':<6} {'Qty':>4} {'Price':>8}")
    print(f"  {'─'*30} {'─'*5} {'─'*4} {'─'*8}")
    for r in rows:
        price_str = f"${r['price_usd']:.2f}" if r['price_usd'] else "N/A"
        print(f"  {r['name'][:30]:<30} {r['set_code']:<6} {r['quantity']:>4} "
              f"{price_str:>8}")
    if total > limit:
        print(f"  ... and {total - limit} more")
    print(f"{'─' * 70}\n")
