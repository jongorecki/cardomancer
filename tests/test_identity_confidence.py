"""
Regression tests for the identity-confidence routing + review-queue
seeding (autonomy ladder roadblock #5).

When a scan's hash distance crosses IDENTITY_LOW_CONFIDENCE_DISTANCE,
the worker overrides the routed bin to the sort_config's fallback bin
and the review-queue seeder creates an 'identity' detection_reviews row
so the user can verify the match later.

These tests cover:
- 'identity' is in DETECTION_VARIABLES.
- seed_detection_reviews_from_scans seeds 'identity' rows ONLY for
  scans with high hash_distance.
- 'identity' confidence column stores hash_distance for queue ordering.
- Confident scans don't pollute the queue.
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
        sys.modules['gcode_control'] = gcode_mock


class IdentityVariableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        import collection_db
        cls.cdb = collection_db

    def test_identity_in_detection_variables(self):
        """Sanity: 'identity' is a recognised review variable."""
        self.assertIn('identity', self.cdb.DETECTION_VARIABLES)


class SeedingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()

    def setUp(self):
        # Fresh DB per test
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        import collection_db
        self.cdb = collection_db
        self._orig_path = collection_db.DB_PATH
        collection_db.DB_PATH = self.tmp.name
        self.conn = collection_db.get_connection()
        # Make sure config.IDENTITY_LOW_CONFIDENCE_DISTANCE is the
        # 90 value the tests below assume. If config has been monkey-
        # patched elsewhere, the test still succeeds — distance values
        # are picked relative to whatever the threshold actually is.
        import config as _cfg
        self.threshold = _cfg.IDENTITY_LOW_CONFIDENCE_DISTANCE

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.cdb.DB_PATH = self._orig_path
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _add_scan(self, name='Test Card', set_code='tst',
                  hash_distance=None, recognized=True):
        sid = self.cdb.start_session(self.conn, sort_mode='color',
                                     config_name='c', bin_count=10)
        cur = self.conn.execute(
            """INSERT INTO scan_history
               (session_id, scan_num, timestamp, name, set_code,
                recognized, bin, hash_distance)
               VALUES (?, 1, ?, ?, ?, ?, 1, ?)""",
            (sid, datetime.now().isoformat(), name, set_code,
             1 if recognized else 0, hash_distance)
        )
        self.conn.commit()
        return cur.lastrowid

    def test_high_confidence_does_not_seed_identity(self):
        """Scan with hash_distance well below threshold → no
        identity review row."""
        self._add_scan(hash_distance=self.threshold - 30)
        self.cdb.seed_detection_reviews_from_scans(
            self.conn, variables=('identity',)
        )
        rows = self.conn.execute(
            "SELECT * FROM detection_reviews WHERE variable='identity'"
        ).fetchall()
        self.assertEqual(len(rows), 0,
                         "Confident scans must not seed 'identity' rows.")

    def test_low_confidence_seeds_identity(self):
        """Scan above threshold → seeded with name and confidence."""
        scan_id = self._add_scan(name='Borderline Card',
                                 hash_distance=self.threshold + 10)
        self.cdb.seed_detection_reviews_from_scans(
            self.conn, variables=('identity',)
        )
        rows = self.conn.execute(
            "SELECT scan_id, detected_value, confidence "
            "FROM detection_reviews WHERE variable='identity'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['scan_id'], scan_id)
        self.assertEqual(rows[0]['detected_value'], 'Borderline Card')
        # Confidence stored as hash distance for ordering
        self.assertAlmostEqual(rows[0]['confidence'],
                               self.threshold + 10, places=2)

    def test_threshold_boundary_is_low_confidence(self):
        """Exactly at threshold counts as low-confidence (>=)."""
        self._add_scan(hash_distance=self.threshold)
        self.cdb.seed_detection_reviews_from_scans(
            self.conn, variables=('identity',)
        )
        rows = self.conn.execute(
            "SELECT * FROM detection_reviews WHERE variable='identity'"
        ).fetchall()
        self.assertEqual(len(rows), 1,
                         "Distance == threshold should seed.")

    def test_null_hash_distance_does_not_seed(self):
        """Older scans with no hash_distance column populated → skip."""
        self._add_scan(hash_distance=None)
        self.cdb.seed_detection_reviews_from_scans(
            self.conn, variables=('identity',)
        )
        rows = self.conn.execute(
            "SELECT * FROM detection_reviews WHERE variable='identity'"
        ).fetchall()
        self.assertEqual(len(rows), 0)

    def test_other_variables_unaffected(self):
        """Adding 'identity' to the seeder must not break the existing
        'foil', 'border', 'set_symbol' seeding behaviour."""
        self._add_scan(set_code='dom', hash_distance=20)  # confident
        self.cdb.seed_detection_reviews_from_scans(
            self.conn, variables=('foil', 'border', 'set_symbol')
        )
        rows = self.conn.execute(
            "SELECT variable, detected_value FROM detection_reviews"
        ).fetchall()
        # 1 scan × 3 variables = 3 rows; identity should be absent
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            sorted(r['variable'] for r in rows),
            ['border', 'foil', 'set_symbol']
        )
        # set_symbol detected_value should still be the set code
        for r in rows:
            if r['variable'] == 'set_symbol':
                self.assertEqual(r['detected_value'], 'dom')


if __name__ == '__main__':
    unittest.main()
