"""
Regression tests for the power-loss / unclean-shutdown recovery flow.

Phase 2 ships detect + discard. These tests pin the contract for both:
- find_stale_sessions returns sessions with end_time IS NULL, ordered
  most-recent-first, with scan_count computed from scan_history.
- _cmd_discard_stale_session marks the session ended with totals
  computed from scan_history (not trusted from any in-memory state).

The full Resume-with-rehydration flow is Phase 4 work; tests for that
will live alongside the staged Sort tab implementation.
"""

import os
import sys
import sqlite3
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime


def _install_hw_mocks():
    """Mock gcode_control so importing web_worker doesn't need a serial
    port. Idempotent."""
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.ser = None
        sys.modules['gcode_control'] = gcode_mock


class StaleSessionDetectionTests(unittest.TestCase):
    """Tests for collection_db.find_stale_sessions."""

    def setUp(self):
        _install_hw_mocks()
        # Use an in-memory copy of the schema by routing collection_db
        # to a temp file. Each test gets a fresh DB.
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

        # Reload collection_db with the temp path
        import collection_db
        self.cdb = collection_db
        self._orig_db_path = collection_db.DB_PATH
        collection_db.DB_PATH = self.db_path
        self.conn = collection_db.get_connection()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.cdb.DB_PATH = self._orig_db_path
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _add_session(self, ended=True, scan_count=0, **kwargs):
        sid = self.cdb.start_session(self.conn, sort_mode='color',
                                     config_name='color.txt', bin_count=10)
        for i in range(1, scan_count + 1):
            self.conn.execute(
                """INSERT INTO scan_history
                   (session_id, scan_num, timestamp, name, recognized, bin)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, i, datetime.now().isoformat(),
                 f'Card {i}', kwargs.get('recognized', 1),
                 kwargs.get('bin', 1))
            )
        if ended:
            self.cdb.end_session(self.conn, sid,
                                 total_scans=scan_count,
                                 recognized=scan_count)
        self.conn.commit()
        return sid

    def test_no_stale_sessions_returns_empty(self):
        """Clean DB with one ended session → no stale rows."""
        self._add_session(ended=True, scan_count=5)
        result = self.cdb.find_stale_sessions(self.conn)
        self.assertEqual(result, [])

    def test_finds_unfinished_session(self):
        """Session with end_time IS NULL → returned by find_stale_sessions."""
        sid = self._add_session(ended=False, scan_count=3)
        result = self.cdb.find_stale_sessions(self.conn)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['id'], sid)
        self.assertEqual(result[0]['scan_count'], 3,
                         "scan_count must be computed from scan_history")

    def test_orders_most_recent_first(self):
        """Multiple stale sessions are ordered by start_time DESC."""
        # Insert in chronological order (older first, then newer)
        s1 = self._add_session(ended=False, scan_count=1)
        # Sleep a tick so the timestamps differ
        import time as _t
        _t.sleep(0.01)
        s2 = self._add_session(ended=False, scan_count=2)
        result = self.cdb.find_stale_sessions(self.conn)
        self.assertEqual([r['id'] for r in result], [s2, s1],
                         "Most recent stale session must be first")

    def test_ended_sessions_excluded(self):
        """Mix of ended and unfinished — only unfinished ones returned."""
        self._add_session(ended=True, scan_count=10)
        sid_stale = self._add_session(ended=False, scan_count=2)
        result = self.cdb.find_stale_sessions(self.conn)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['id'], sid_stale)

    def test_get_session_bin_counts_groups_correctly(self):
        """Reconstruction of per-bin counts from scan_history."""
        sid = self.cdb.start_session(self.conn, sort_mode='custom_file',
                                     config_name='test', bin_count=5)
        # Add scans across three bins
        for bin_num, count in [(1, 4), (2, 7), (5, 1)]:
            for i in range(count):
                self.conn.execute(
                    """INSERT INTO scan_history
                       (session_id, scan_num, timestamp, name, recognized, bin)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (sid, i, datetime.now().isoformat(),
                     f'C', 1, bin_num)
                )
        self.conn.commit()
        result = self.cdb.get_session_bin_counts(self.conn, sid)
        self.assertEqual(result, {1: 4, 2: 7, 5: 1})


class StaleSessionDiscardTests(unittest.TestCase):
    """Tests for web_worker._cmd_discard_stale_session."""

    def setUp(self):
        _install_hw_mocks()
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

        import collection_db
        self.cdb = collection_db
        self._orig_db_path = collection_db.DB_PATH
        collection_db.DB_PATH = self.db_path
        self.conn = collection_db.get_connection()

        import web_worker
        self.worker = web_worker.SortWorker()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.cdb.DB_PATH = self._orig_db_path
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_discard_marks_session_ended_with_computed_totals(self):
        """_cmd_discard_stale_session should stamp end_time AND populate
        total/recognized/unrecognized from scan_history."""
        sid = self.cdb.start_session(self.conn, sort_mode='color',
                                     config_name='color.txt', bin_count=10)
        # 5 scans: 4 recognized, 1 not
        from datetime import datetime as _dt
        for i in range(5):
            self.conn.execute(
                """INSERT INTO scan_history
                   (session_id, scan_num, timestamp, name, recognized, bin)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, i, _dt.now().isoformat(),
                 f'C{i}', 1 if i < 4 else 0, 1)
            )
        self.conn.commit()

        # Sanity: stale before discard
        self.assertEqual(len(self.cdb.find_stale_sessions(self.conn)), 1)

        self.worker._cmd_discard_stale_session(session_id=sid)

        # No more stale rows
        self.assertEqual(self.cdb.find_stale_sessions(self.conn), [])

        # Session row has correct totals
        meta = self.cdb.get_session_metadata(self.conn, sid)
        self.assertIsNotNone(meta['end_time'])
        self.assertEqual(meta['total_scans'], 5)
        self.assertEqual(meta['recognized'], 4)
        self.assertEqual(meta['unrecognized'], 1)

    def test_discard_with_missing_session_id_logs_and_returns(self):
        """No session_id → log and bail, no crash."""
        # Should not raise
        self.worker._cmd_discard_stale_session(session_id=None)


if __name__ == '__main__':
    unittest.main()
