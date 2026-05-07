"""
Regression tests for the source-bin count estimator.

Backend feature: every time the source bin is probed, the worker
compares the probed Z to a calibrated empty-bin reference Z and divides
by config.CARD_THICKNESS_MM to estimate cards remaining. A calibration
command persists the empty-Z reference to disk for cross-restart
durability.

These tests cover:
- The pure estimate helper (no hardware, no IO)
- The persistence cycle (save → load round-trip, missing file → None)
"""

import sys
import json
import os
import tempfile
import unittest
import unittest.mock as mock


def _install_hw_mocks():
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.ser = None
        sys.modules['gcode_control'] = gcode_mock


class EstimateHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        import web_worker
        cls.worker_cls = web_worker.SortWorker

    def test_returns_none_when_uncalibrated(self):
        """No empty-Z reference → can't estimate; return None."""
        self.assertIsNone(self.worker_cls._estimate_source_count(50.0, None))

    def test_zero_when_probed_at_or_below_empty(self):
        """Probed Z at empty reference (or below — sensor jitter) → 0
        cards. Never negative."""
        # Exactly at empty
        self.assertEqual(self.worker_cls._estimate_source_count(20.0, 20.0), 0)
        # Below empty (jitter / bin moved)
        self.assertEqual(self.worker_cls._estimate_source_count(15.0, 20.0), 0)

    def test_estimate_with_unsleeved_thickness(self):
        """Standard MTG card = 0.305mm. 30.5mm above empty = 100 cards."""
        # empty=20mm, probed=50.5mm → delta=30.5 → 30.5/0.305 = 100
        self.assertEqual(
            self.worker_cls._estimate_source_count(50.5, 20.0), 100
        )

    def test_estimate_rounds_to_nearest(self):
        """Fractional results round to the nearest integer."""
        # delta=0.5mm → 0.5/0.305 ≈ 1.64 → rounds to 2
        self.assertEqual(
            self.worker_cls._estimate_source_count(20.5, 20.0), 2
        )
        # delta=0.45mm → 0.45/0.305 ≈ 1.475 → rounds to 1
        self.assertEqual(
            self.worker_cls._estimate_source_count(20.45, 20.0), 1
        )


class PersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        import web_worker
        cls.worker_cls = web_worker.SortWorker

    def setUp(self):
        # Redirect EMPTY_SOURCE_BIN_REF_PATH to a temp file
        import config
        self._orig_path = config.EMPTY_SOURCE_BIN_REF_PATH
        self._tmp = tempfile.NamedTemporaryFile(
            suffix='.json', delete=False
        )
        self._tmp.close()
        # Pre-delete so "no file yet" tests start clean
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass
        config.EMPTY_SOURCE_BIN_REF_PATH = self._tmp.name
        self.worker = self.worker_cls()

    def tearDown(self):
        import config
        config.EMPTY_SOURCE_BIN_REF_PATH = self._orig_path
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_load_returns_none_when_no_file(self):
        """Fresh install — no calibration file yet."""
        self.assertIsNone(self.worker._load_empty_source_z())

    def test_save_and_load_round_trip(self):
        self.worker._save_empty_source_z(42.5)
        loaded = self.worker._load_empty_source_z()
        self.assertEqual(loaded, 42.5)

    def test_save_writes_calibrated_at_timestamp(self):
        """Persisted file should include an ISO timestamp so the user
        knows when calibration was last run."""
        self.worker._save_empty_source_z(10.0)
        with open(self._tmp.name, 'r') as f:
            data = json.load(f)
        self.assertIn('empty_z', data)
        self.assertIn('calibrated_at', data)
        self.assertEqual(data['empty_z'], 10.0)

    def test_load_handles_corrupt_file(self):
        """A malformed JSON file must not crash startup; return None."""
        with open(self._tmp.name, 'w') as f:
            f.write('not valid json{{{')
        self.assertIsNone(self.worker._load_empty_source_z())


if __name__ == '__main__':
    unittest.main()
