# scan_tracker.py
# ---------------------------------------------------------------------------
# Tracks all scanned cards during a sorting session.
#
# Writes to two destinations:
#   1. Session log files (CSV, JSON, TXT) — for quick per-session review
#   2. SQLite collection database — for long-term collection tracking
#
# Every recognized card is automatically added to the collection. If it
# already exists, its quantity is incremented.
# ---------------------------------------------------------------------------

import os
import csv
import json
from datetime import datetime

from config import SCAN_LOGS_DIR
import collection_db


class ScanTracker:
    """
    Tracks cards scanned during a sorting session.

    Each session gets:
      - A timestamped directory under scan_logs/ with CSV, JSON, and TXT files
      - Records in the SQLite collection database (collection.db)
    """

    def __init__(self):
        self.session_dir = None
        self.session_meta = {}
        self.scans = []
        self.bins = {}
        self.scan_count = 0
        self.unrecognized_count = 0
        self._csv_writer = None
        self._csv_file = None
        self._db_conn = None
        self._db_session_id = None

    def start_session(self, sort_mode=None, config_name=None, bin_count=None,
                      extra_meta=None, notes=None):
        """
        Start a new tracking session.

        :param sort_mode:    Sorting mode name (e.g. "color", "custom_file")
        :param config_name:  Name of the sort config file (if applicable)
        :param bin_count:    Number of bins configured
        :param extra_meta:   Dict of any additional metadata to record
        """
        # --- Session log directory ---
        os.makedirs(SCAN_LOGS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = os.path.join(SCAN_LOGS_DIR, f"session_{timestamp}")
        os.makedirs(self.session_dir, exist_ok=True)

        self.session_meta = {
            "start_time": datetime.now().isoformat(),
            "sort_mode": sort_mode,
            "config_name": config_name,
            "bin_count": bin_count,
        }
        if extra_meta:
            self.session_meta.update(extra_meta)
        self._save_session_meta()

        # --- CSV log ---
        csv_path = os.path.join(self.session_dir, "scans.csv")
        self._csv_file = open(csv_path, 'w', newline='', encoding='utf-8')
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow([
            "scan_num", "timestamp", "name", "set", "all_sets",
            "collector_number", "colors", "cmc", "type_line", "rarity",
            "price_usd", "bin", "method", "hash_distance", "recognized",
            "is_foil", "foil_confidence"
        ])

        # --- SQLite collection database ---
        self._db_conn = collection_db.get_connection()
        self._db_session_id = collection_db.start_session(
            self._db_conn,
            sort_mode=sort_mode,
            config_name=config_name,
            bin_count=bin_count,
            notes=notes,
        )

        self.scans = []
        self.bins = {}
        self.scan_count = 0
        self.unrecognized_count = 0

        print(f"[tracker] Session started: {self.session_dir}")

    def record_scan(self, card_info=None, bin_num=None, method=None,
                    hash_distance=None, card_data=None,
                    is_foil=False, foil_confidence=None):
        """
        Record a single card scan to session logs and collection DB.

        :param is_foil:         bool, set by foil_detect post-identification
        :param foil_confidence: float, raw foil detection confidence score
        """
        if self.session_dir is None:
            print("[tracker] Warning: no active session, call start_session() first")
            return

        self.scan_count += 1
        timestamp = datetime.now().isoformat()
        recognized = card_info is not None

        if not recognized:
            self.unrecognized_count += 1

        # Extract fields
        name = card_info.get('Name', '') if card_info else 'Unrecognized'
        set_code = card_info.get('Set', '') if card_info else ''
        all_sets = card_info.get('Sets', []) if card_info else []
        colors = card_info.get('Colors', []) if card_info else []
        cmc = card_info.get('CMC', '') if card_info else ''
        types = card_info.get('Types', '') if card_info else ''
        rarity = card_info.get('Rarity', '') if card_info else ''
        price = card_info.get('Price', '') if card_info else ''

        collector_number = ''
        type_line = ''
        if card_data:
            collector_number = card_data.get('collector_number', '')
            type_line = card_data.get('type_line', types)

        # --- Session scan record ---
        scan_record = {
            "scan_num": self.scan_count,
            "timestamp": timestamp,
            "name": name,
            "set": set_code,
            "all_sets": all_sets,
            "collector_number": collector_number,
            "colors": colors,
            "cmc": cmc,
            "type_line": type_line,
            "rarity": rarity,
            "price_usd": price,
            "bin": bin_num,
            "method": method or '',
            "hash_distance": hash_distance,
            "recognized": recognized,
            "is_foil": bool(is_foil),
            "foil_confidence": foil_confidence,
        }
        self.scans.append(scan_record)

        # --- Write CSV row ---
        if self._csv_writer:
            self._csv_writer.writerow([
                self.scan_count, timestamp, name, set_code,
                ';'.join(all_sets) if all_sets else '',
                collector_number,
                ''.join(colors) if colors else '',
                cmc, type_line, rarity, price,
                bin_num if bin_num is not None else '',
                method or '',
                f"{hash_distance:.2f}" if hash_distance is not None else '',
                recognized,
                int(bool(is_foil)),
                f"{foil_confidence:.3f}" if foil_confidence is not None else '',
            ])
            self._csv_file.flush()

        # --- Update session bin tracking ---
        if bin_num is not None:
            bin_key = str(bin_num)
            if bin_key not in self.bins:
                self.bins[bin_key] = []
            self.bins[bin_key].append({
                "name": name,
                "set": set_code,
                "collector_number": collector_number,
                "scan_num": self.scan_count,
            })
            self._save_bins()

        # --- Write to collection database ---
        if self._db_conn and self._db_session_id:
            collection_db.record_scan(
                self._db_conn,
                session_id=self._db_session_id,
                scan_num=self.scan_count,
                card_info=card_info,
                card_data=card_data,
                bin_num=bin_num,
                method=method,
                hash_distance=hash_distance,
                is_foil=is_foil,
                foil_confidence=foil_confidence,
            )

        # Print status
        if recognized:
            print(f"[tracker] Scan #{self.scan_count}: {name} ({set_code}) "
                  f"-> bin {bin_num}")
        else:
            print(f"[tracker] Scan #{self.scan_count}: UNRECOGNIZED -> bin {bin_num}")

    def get_bin_contents(self, bin_num):
        """Get list of cards in a specific bin (this session)."""
        return self.bins.get(str(bin_num), [])

    def get_all_bins(self):
        """Get the full bin inventory dict (this session)."""
        return dict(self.bins)

    def get_stats(self):
        """Get session statistics."""
        total_value = 0.0
        for scan in self.scans:
            try:
                price = scan.get('price_usd', '')
                if price and price != 'N/A':
                    total_value += float(str(price).replace('$', ''))
            except (ValueError, TypeError):
                pass

        return {
            "total_scans": self.scan_count,
            "recognized": self.scan_count - self.unrecognized_count,
            "unrecognized": self.unrecognized_count,
            "recognition_rate": (
                f"{(self.scan_count - self.unrecognized_count) / self.scan_count * 100:.1f}%"
                if self.scan_count > 0 else "N/A"
            ),
            "bins_used": len(self.bins),
            "cards_per_bin": {
                k: len(v) for k, v in sorted(self.bins.items(), key=lambda x: int(x[0]))
            },
            "total_value": f"${total_value:.2f}",
        }

    def print_status(self):
        """Print current session status to console."""
        stats = self.get_stats()
        print(f"\n{'─' * 50}")
        print(f"  Session Status")
        print(f"{'─' * 50}")
        print(f"  Total scans:      {stats['total_scans']}")
        print(f"  Recognized:       {stats['recognized']}")
        print(f"  Unrecognized:     {stats['unrecognized']}")
        print(f"  Recognition rate: {stats['recognition_rate']}")
        print(f"  Total value:      {stats['total_value']}")
        print(f"  Bins used:        {stats['bins_used']}")
        for bin_num, count in stats['cards_per_bin'].items():
            print(f"    Bin {bin_num}: {count} cards")
        print(f"{'─' * 50}\n")

    def print_bin(self, bin_num):
        """Print contents of a specific bin (this session)."""
        contents = self.get_bin_contents(bin_num)
        print(f"\n{'─' * 50}")
        print(f"  Bin {bin_num} — {len(contents)} cards")
        print(f"{'─' * 50}")
        for card in contents:
            print(f"  #{card['scan_num']:3d}: {card['name']} "
                  f"({card['set']}/{card['collector_number']})")
        print(f"{'─' * 50}\n")

    def print_collection_stats(self):
        """Print overall collection stats from the database."""
        if self._db_conn:
            collection_db.print_collection_stats(self._db_conn)

    def print_inventory(self, limit=20):
        """Print inventory summary from the database."""
        if self._db_conn:
            collection_db.print_inventory_summary(self._db_conn, limit=limit)

    def end_session(self):
        """End the current session. Writes final summary and closes files."""
        if self.session_dir is None:
            return

        self.session_meta["end_time"] = datetime.now().isoformat()
        self.session_meta["total_scans"] = self.scan_count
        self.session_meta["recognized"] = self.scan_count - self.unrecognized_count
        self.session_meta["unrecognized"] = self.unrecognized_count
        self._save_session_meta()
        self._save_bins()
        self._write_summary()

        # Close CSV
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

        # Update and close database session
        if self._db_conn and self._db_session_id:
            collection_db.end_session(
                self._db_conn, self._db_session_id,
                total_scans=self.scan_count,
                recognized=self.scan_count - self.unrecognized_count,
                unrecognized=self.unrecognized_count,
            )
            self._db_conn.close()
            self._db_conn = None

        print(f"[tracker] Session ended. {self.scan_count} cards scanned.")
        print(f"[tracker] Logs saved to: {self.session_dir}")

    def _save_session_meta(self):
        path = os.path.join(self.session_dir, "session.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.session_meta, f, indent=2, ensure_ascii=False)

    def _save_bins(self):
        path = os.path.join(self.session_dir, "bins.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.bins, f, indent=2, ensure_ascii=False)

    def _write_summary(self):
        path = os.path.join(self.session_dir, "summary.txt")
        stats = self.get_stats()

        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"MTG Card Sorter — Session Summary\n")
            f.write(f"{'=' * 50}\n\n")
            f.write(f"Start:    {self.session_meta.get('start_time', '?')}\n")
            f.write(f"End:      {self.session_meta.get('end_time', '?')}\n")
            f.write(f"Mode:     {self.session_meta.get('sort_mode', '?')}\n")
            if self.session_meta.get('config_name'):
                f.write(f"Config:   {self.session_meta['config_name']}\n")
            f.write(f"\n")
            f.write(f"Total scans:      {stats['total_scans']}\n")
            f.write(f"Recognized:       {stats['recognized']}\n")
            f.write(f"Unrecognized:     {stats['unrecognized']}\n")
            f.write(f"Recognition rate: {stats['recognition_rate']}\n")
            f.write(f"Total value:      {stats['total_value']}\n")
            f.write(f"\n")

            # Bin breakdown
            f.write(f"{'=' * 50}\n")
            f.write(f"Bin Contents\n")
            f.write(f"{'=' * 50}\n\n")
            for bin_num in sorted(self.bins.keys(), key=lambda x: int(x)):
                cards = self.bins[bin_num]
                f.write(f"Bin {bin_num} ({len(cards)} cards)\n")
                f.write(f"{'-' * 40}\n")
                for card in cards:
                    name = card.get('name', '?')
                    sset = card.get('set', '?')
                    cnum = card.get('collector_number', '?')
                    f.write(f"  {name} ({sset}/{cnum})\n")
                f.write(f"\n")

            # Full scan log
            f.write(f"{'=' * 50}\n")
            f.write(f"Full Scan Log\n")
            f.write(f"{'=' * 50}\n\n")
            for scan in self.scans:
                num = scan['scan_num']
                name = scan['name'] or 'UNRECOGNIZED'
                sset = scan['set']
                bbin = scan['bin']
                method = scan['method']
                dist = scan['hash_distance']
                dist_str = f"dist={dist:.1f}" if dist is not None else ""
                f.write(f"  #{num:3d}: {name} ({sset}) -> bin {bbin} "
                        f"[{method}] {dist_str}\n")


# ---------------------------------------------------------------------------
# Standalone session review functions
# ---------------------------------------------------------------------------

def load_session(session_dir):
    """Load a past session for review."""
    try:
        meta_path = os.path.join(session_dir, "session.json")
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)

        bins_path = os.path.join(session_dir, "bins.json")
        bins = {}
        if os.path.exists(bins_path):
            with open(bins_path, 'r', encoding='utf-8') as f:
                bins = json.load(f)

        scans = []
        csv_path = os.path.join(session_dir, "scans.csv")
        if os.path.exists(csv_path):
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    scans.append(row)

        return meta, scans, bins
    except Exception as e:
        print(f"[tracker] Error loading session: {e}")
        return None


def list_sessions():
    """List all past sessions with basic info."""
    if not os.path.exists(SCAN_LOGS_DIR):
        print("[tracker] No scan logs found.")
        return []

    sessions = []
    for name in sorted(os.listdir(SCAN_LOGS_DIR)):
        session_dir = os.path.join(SCAN_LOGS_DIR, name)
        if not os.path.isdir(session_dir):
            continue
        meta_path = os.path.join(session_dir, "session.json")
        if not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            sessions.append({
                "dir": session_dir,
                "name": name,
                "start_time": meta.get("start_time", "?"),
                "sort_mode": meta.get("sort_mode", "?"),
                "total_scans": meta.get("total_scans", "?"),
            })
        except Exception:
            continue

    if not sessions:
        print("[tracker] No sessions found.")
    else:
        print(f"\n{'─' * 70}")
        print(f"  Past Sessions")
        print(f"{'─' * 70}")
        for i, s in enumerate(sessions, 1):
            print(f"  {i}. {s['name']}  |  {s['sort_mode']}  |  "
                  f"{s['total_scans']} cards  |  {s['start_time']}")
        print(f"{'─' * 70}\n")

    return sessions
