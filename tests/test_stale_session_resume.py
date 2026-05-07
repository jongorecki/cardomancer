"""
Regression tests for Phase 4 part 5 — full power-loss-resume rehydration.

Builds on the detect+discard work from roadblock #3. These tests pin
the rehydration contract:

- ScanTracker.resume_session() loads metadata from the existing
  session row, rebuilds bins/scan_count from scan_history, opens a
  fresh resume_<timestamp>_resumed_<id> log directory, returns True.
- _cmd_resume_stale_session rebuilds sort_config_obj from the saved
  config file, restores tracker, and lands the worker in 'paused'.
- Sessions started with inline config_lines (no config_name) cannot
  be resumed — the worker emits an error event with a clear message.
- Sessions whose config_name no longer exists on disk also error
  cleanly rather than silently failing.
"""

import os
import sys
import sqlite3
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime


def _install_hw_mocks():
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.ser = None
        gcode_mock.get_bin_locations = lambda: {}
        sys.modules['gcode_control'] = gcode_mock


class ResumeSessionTrackerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp_db.close()
        self.tmp_logs = tempfile.mkdtemp(prefix='cm_resume_logs_')

        import collection_db
        self.cdb = collection_db
        self._orig_db_path = collection_db.DB_PATH
        collection_db.DB_PATH = self.tmp_db.name

        # Redirect SCAN_LOGS_DIR for the tracker
        import scan_tracker
        self.tracker_mod = scan_tracker
        self._orig_scan_logs_dir = scan_tracker.SCAN_LOGS_DIR
        scan_tracker.SCAN_LOGS_DIR = self.tmp_logs

        self.conn = collection_db.get_connection()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.cdb.DB_PATH = self._orig_db_path
        self.tracker_mod.SCAN_LOGS_DIR = self._orig_scan_logs_dir
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass
        import shutil
        shutil.rmtree(self.tmp_logs, ignore_errors=True)

    def _stale_session_with_scans(self, n_scans=5, n_recognized=4,
                                   config_name='color.txt'):
        sid = self.cdb.start_session(
            self.conn, sort_mode='color', config_name=config_name,
            bin_count=10
        )
        for i in range(n_scans):
            self.conn.execute(
                """INSERT INTO scan_history
                   (session_id, scan_num, timestamp, name, recognized, bin)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, i, datetime.now().isoformat(),
                 f'C{i}', 1 if i < n_recognized else 0,
                 (i % 3) + 1)
            )
        self.conn.commit()
        return sid

    def test_resume_returns_true_for_stale_session(self):
        sid = self._stale_session_with_scans(n_scans=5)
        tracker = self.tracker_mod.ScanTracker()
        ok = tracker.resume_session(sid)
        self.assertTrue(ok)

    def test_resume_rebuilds_scan_count_from_history(self):
        sid = self._stale_session_with_scans(n_scans=7)
        tracker = self.tracker_mod.ScanTracker()
        tracker.resume_session(sid)
        self.assertEqual(tracker.scan_count, 7)

    def test_resume_rebuilds_unrecognized_count(self):
        sid = self._stale_session_with_scans(n_scans=5, n_recognized=3)
        tracker = self.tracker_mod.ScanTracker()
        tracker.resume_session(sid)
        self.assertEqual(tracker.unrecognized_count, 2)

    def test_resume_creates_new_log_dir(self):
        sid = self._stale_session_with_scans(n_scans=3)
        tracker = self.tracker_mod.ScanTracker()
        tracker.resume_session(sid)
        self.assertTrue(os.path.isdir(tracker.session_dir))
        self.assertIn('_resumed_', tracker.session_dir)
        # session.json exists with the resumed-from marker
        meta_path = os.path.join(tracker.session_dir, 'session.json')
        self.assertTrue(os.path.exists(meta_path))
        import json
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        self.assertEqual(meta['resumed_from_session_id'], sid)
        self.assertIn('resumed_at', meta)

    def test_resume_keeps_existing_db_session_id(self):
        """Crucial: rehydration MUST reuse the existing session row, not
        create a new one. Otherwise scan_history would split across two
        rows and totals would be wrong."""
        sid = self._stale_session_with_scans(n_scans=3)
        tracker = self.tracker_mod.ScanTracker()
        tracker.resume_session(sid)
        self.assertEqual(tracker._db_session_id, sid)

        # And the stale row in `sessions` should NOT have an end_time —
        # we resumed, we didn't end.
        row = self.conn.execute(
            "SELECT end_time FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        self.assertIsNone(row['end_time'])

    def test_resume_returns_false_for_missing_session(self):
        tracker = self.tracker_mod.ScanTracker()
        ok = tracker.resume_session(99999)
        self.assertFalse(ok)

    def test_resume_returns_false_for_already_ended(self):
        sid = self._stale_session_with_scans(n_scans=3)
        # End the session normally
        self.cdb.end_session(self.conn, sid, total_scans=3,
                             recognized=3, unrecognized=0)
        tracker = self.tracker_mod.ScanTracker()
        ok = tracker.resume_session(sid)
        self.assertFalse(ok)


class ResumeWorkerCommandTests(unittest.TestCase):
    """_cmd_resume_stale_session glue layer — sort_config rebuild,
    state transition, error paths."""

    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp_db.close()
        self.tmp_logs = tempfile.mkdtemp(prefix='cm_worker_resume_')

        import collection_db
        self.cdb = collection_db
        self._orig_db_path = collection_db.DB_PATH
        collection_db.DB_PATH = self.tmp_db.name

        import scan_tracker
        self.tracker_mod = scan_tracker
        self._orig_scan_logs_dir = scan_tracker.SCAN_LOGS_DIR
        scan_tracker.SCAN_LOGS_DIR = self.tmp_logs

        self.conn = collection_db.get_connection()

        import web_worker
        self.worker = web_worker.SortWorker()
        self.emitted = []
        self.worker._emit_fn = lambda ev, data: self.emitted.append((ev, data))

    def tearDown(self):
        self.conn.close()
        self.cdb.DB_PATH = self._orig_db_path
        self.tracker_mod.SCAN_LOGS_DIR = self._orig_scan_logs_dir
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass
        import shutil
        shutil.rmtree(self.tmp_logs, ignore_errors=True)

    def _last_event(self, name):
        for ev, data in reversed(self.emitted):
            if ev == name:
                return data
        return None

    def _stale_session(self, config_name='color.txt'):
        sid = self.cdb.start_session(
            self.conn, sort_mode='color', config_name=config_name,
            bin_count=10,
        )
        # 4 scans across 2 bins
        for i in range(4):
            self.conn.execute(
                """INSERT INTO scan_history
                   (session_id, scan_num, timestamp, name, recognized, bin)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, i, datetime.now().isoformat(),
                 f'C{i}', 1, (i % 2) + 1)
            )
        self.conn.commit()
        return sid

    def test_missing_session_id_no_op(self):
        self.worker._cmd_resume_stale_session(session_id=None)
        # No error should be emitted (the implementation just logs and returns)
        # but a missing session_id ALSO shouldn't crash.
        self.assertIsNone(self._last_event('stale_session_resumed'))

    def test_resume_with_active_session_errors(self):
        sid = self._stale_session()
        self.worker._state = 'sorting'
        self.worker._cmd_resume_stale_session(session_id=sid)
        err = self._last_event('error')
        self.assertIsNotNone(err)
        self.assertIn('already in flight', err['message'])

    def test_resume_with_missing_config_name_errors(self):
        sid = self._stale_session(config_name='')
        self.worker._cmd_resume_stale_session(session_id=sid)
        err = self._last_event('error')
        self.assertIsNotNone(err)
        self.assertIn('inline configuration', err['message'].lower())

    def test_resume_with_missing_config_file_errors(self):
        sid = self._stale_session(config_name='nonexistent_preset.txt')
        self.worker._cmd_resume_stale_session(session_id=sid)
        err = self._last_event('error')
        self.assertIsNotNone(err)
        self.assertIn('no longer exists', err['message'])

    def test_resume_already_ended_errors(self):
        sid = self._stale_session()
        self.cdb.end_session(self.conn, sid, total_scans=4,
                             recognized=4, unrecognized=0)
        self.worker._cmd_resume_stale_session(session_id=sid)
        err = self._last_event('error')
        self.assertIsNotNone(err)
        self.assertIn('already ended', err['message'])

    def test_successful_resume_full_path(self):
        """End-to-end: real saved config file (color.txt exists in
        sort_configs/), no end_time, idle worker. Should land in paused
        with stale_session_resumed emitted."""
        sid = self._stale_session(config_name='color.txt')
        self.worker._state = 'idle'
        self.worker._cmd_resume_stale_session(session_id=sid)

        # Should have NOT errored
        err = self._last_event('error')
        self.assertIsNone(err, f"unexpected error: {err}")

        # Should have emitted stale_session_resumed
        resumed = self._last_event('stale_session_resumed')
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed['session_id'], sid)
        self.assertEqual(resumed['scan_count'], 4)

        # Worker should be paused
        self.assertEqual(self.worker._state, 'paused')

        # Tracker should be rehydrated
        self.assertIsNotNone(self.worker.tracker)
        self.assertEqual(self.worker.tracker.scan_count, 4)


if __name__ == '__main__':
    unittest.main()
