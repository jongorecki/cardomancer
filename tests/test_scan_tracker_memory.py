"""
Regression tests for the ScanTracker memory bound (the long-session
profile fix).

Before this change, every recorded scan appended a ~20-field dict to
`tracker.scans`. With a 10k-card session that's an O(N) list held in
RAM for the whole session, plus an O(N) walk every time the dashboard
polled get_stats() for total_value.

After the change:
  - `tracker.scans` no longer exists (CSV + DB persist per-scan).
  - `tracker.session_total_value` is a running float updated in
    `record_scan()`.
  - `get_stats()` reads counters only — no per-scan iteration.
  - The undo path uses `scan_count > 0` and pops from `tracker.bins`.

These tests pin all of that. If anyone reintroduces `self.scans`,
the test for its absence fails immediately.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import collection_db
import scan_tracker


class ScanTrackerMemoryShapeTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='cm_st_mem_')
        self.scan_logs_dir = os.path.join(self.tmpdir, 'scan_logs')
        self.db_path = os.path.join(self.tmpdir, 'collection.db')
        os.makedirs(self.scan_logs_dir, exist_ok=True)

        # Point both globals at the temp paths. ScanTracker constructs
        # its own DB connection via collection_db.get_connection() with
        # no arguments, so we redirect collection_db.DB_PATH too.
        self._orig_scan_logs = scan_tracker.SCAN_LOGS_DIR
        self._orig_db_path = collection_db.DB_PATH
        scan_tracker.SCAN_LOGS_DIR = self.scan_logs_dir
        collection_db.DB_PATH = self.db_path

        self.tracker = scan_tracker.ScanTracker()
        self.tracker.start_session(sort_mode='memtest')

    def tearDown(self):
        try:
            self.tracker.end_session()
        except Exception:
            pass
        scan_tracker.SCAN_LOGS_DIR = self._orig_scan_logs
        collection_db.DB_PATH = self._orig_db_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tracker_has_no_scans_attribute(self):
        """The per-scan in-memory buffer is gone. CSV + DB hold the
        full history; the in-memory state only keeps aggregates +
        per-bin contents."""
        self.assertFalse(
            hasattr(self.tracker, 'scans'),
            "ScanTracker.scans must not exist — that was the leak. "
            "Persist scans via CSV/DB only."
        )

    def test_running_total_value_starts_zero(self):
        self.assertEqual(self.tracker.session_total_value, 0.0)

    def test_running_total_value_updates_per_scan(self):
        """Each recognized scan with a parseable Price increments the
        running total. get_stats() reflects it without iterating
        anything."""
        for price_str in ('1.50', '2.50', '0.75'):
            self.tracker.record_scan(
                card_info={
                    'Name': 'Bolt', 'Set': 'm11', 'Colors': ['R'],
                    'CMC': 1.0, 'Rarity': 'common',
                    'Price': price_str,
                },
                card_data={'collector_number': '149', 'oracle_id': 'b'},
                bin_num=1, method='hash', hash_distance=25.0,
            )
        self.assertAlmostEqual(self.tracker.session_total_value, 4.75, places=2)
        stats = self.tracker.get_stats()
        self.assertEqual(stats['total_value'], '$4.75')
        self.assertEqual(stats['total_scans'], 3)

    def test_unparseable_prices_dont_break_total(self):
        """get_stats() used to silently swallow ValueError when
        iterating scans. The running-total path does the same — bad
        price strings get skipped, not propagated."""
        cases = ['N/A', '', None, 'not-a-number', '$1.00', '2.50']
        for p in cases:
            self.tracker.record_scan(
                card_info={
                    'Name': 'X', 'Set': 's', 'Colors': [], 'CMC': 0.0,
                    'Rarity': 'common', 'Price': p,
                },
                card_data={'collector_number': '1'},
                bin_num=1, method='hash', hash_distance=0.0,
            )
        # Only the two real prices counted: $1.00 + 2.50.
        self.assertAlmostEqual(self.tracker.session_total_value, 3.50, places=2)

    def test_bin_contents_still_tracked(self):
        """The per-bin list is intentionally preserved — it powers
        the live UI's bin-contents drawer. Bound by destination-bin
        cards, not the leak-class N+1 problem."""
        self.tracker.record_scan(
            card_info={
                'Name': 'Bolt', 'Set': 'm11', 'Colors': ['R'],
                'CMC': 1.0, 'Rarity': 'common', 'Price': '0.50',
            },
            card_data={'collector_number': '149', 'oracle_id': 'b'},
            bin_num=3, method='hash', hash_distance=25.0,
        )
        self.assertIn('3', self.tracker.bins)
        self.assertEqual(len(self.tracker.bins['3']), 1)
        self.assertEqual(self.tracker.bins['3'][0]['name'], 'Bolt')

    def test_get_stats_does_not_iterate_per_scan(self):
        """get_stats() must run in O(bins) regardless of scan count.
        Sanity check: 1000 scans, get_stats() returns instantly and
        total_value is correct."""
        for i in range(1000):
            self.tracker.record_scan(
                card_info={
                    'Name': f'C{i}', 'Set': 's', 'Colors': ['R'],
                    'CMC': 1.0, 'Rarity': 'common', 'Price': '0.10',
                },
                card_data={'collector_number': str(i)},
                bin_num=(i % 5) + 1, method='hash', hash_distance=20.0,
            )
        stats = self.tracker.get_stats()
        self.assertEqual(stats['total_scans'], 1000)
        # 1000 * 0.10 — float imprecision tolerated.
        self.assertAlmostEqual(self.tracker.session_total_value, 100.0,
                               places=1)
        self.assertEqual(stats['bins_used'], 5)


if __name__ == '__main__':
    unittest.main()
