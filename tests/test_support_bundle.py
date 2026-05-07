"""
Regression tests for the support bundle generator.

The bundle is the first thing we ask for in any support thread, so the
contract is load-bearing:
- It builds a valid zip that opens without errors.
- manifest.json + app_state.json + README.txt are always present.
- Logs and recent scan_logs sessions are included when they exist.
- Frame directories are NOT included (would balloon bundle size).
- collection.db / hash DBs / large data files are NOT included.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
import zipfile


def _install_hw_mocks():
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.ser = None
        gcode_mock.SERIAL_PORT = 'COM3'
        gcode_mock.X_SOURCE_BIN = 547.1
        gcode_mock.X_STAGING_POSITION = 427.9
        gcode_mock.CAMERA_X_OFFSET = 100.0
        gcode_mock.get_bin_locations = lambda: {0: 547.1, 1: 100.0}
        gcode_mock._last_serial_error = None
        sys.modules['gcode_control'] = gcode_mock


class FakeWorker:
    """Minimal worker stand-in. The bundle reads attributes defensively
    so missing fields fall back to None — we test both populated and
    bare workers."""
    def __init__(self):
        self._state = 'idle'
        self.tracker = None
        self.continuous_sorting = False
        self.scan_count = 0
        self.bins_full = set()
        self.bin_card_counts = {}
        self.overflow_map = {1: [1, 2]}


class SupportBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        import support_bundle
        cls.sb = support_bundle

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='cm_bundle_test_')
        # Pretend repo layout
        os.makedirs(os.path.join(self.repo, 'logs'))
        os.makedirs(os.path.join(self.repo, 'scan_logs'))
        # An app log
        with open(os.path.join(self.repo, 'logs', 'card_sorter.log'),
                  'w', encoding='utf-8') as f:
            f.write('2026-05-07 [INFO] this is a test log line\n')
        # A scan-session directory with metadata + a frames/ subdir
        # (frames/ should be excluded)
        sess = os.path.join(self.repo, 'scan_logs', 'session_20260507_120000')
        os.makedirs(os.path.join(sess, 'frames'))
        with open(os.path.join(sess, 'session.json'), 'w') as f:
            json.dump({'start_time': 'x', 'sort_mode': 'color'}, f)
        with open(os.path.join(sess, 'frames', 'frame_0001.jpg'), 'wb') as f:
            f.write(b'\x00' * 100)
        # A known project file
        with open(os.path.join(self.repo, '_last_setup.json'),
                  'w', encoding='utf-8') as f:
            f.write('{"dest": [], "source": []}')
        # Files that should NOT be included
        with open(os.path.join(self.repo, 'collection.db'), 'wb') as f:
            f.write(b'sensitive collection data')
        with open(os.path.join(self.repo, 'card_hashes_v3.json'), 'w') as f:
            f.write('{"giant": "hash db"}')

        self.worker = FakeWorker()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _build_and_open(self):
        data = self.sb.build_support_bundle(
            repo_root=self.repo, worker=self.worker, app_name='Cardomancer'
        )
        self.assertGreater(len(data), 0, "Bundle was empty")
        zf = zipfile.ZipFile(io.BytesIO(data))
        return zf

    def _names(self, zf):
        return [n for n in zf.namelist()]

    def test_bundle_is_valid_zip(self):
        zf = self._build_and_open()
        # ZipFile.testzip returns None on success, name of first bad file otherwise
        self.assertIsNone(zf.testzip())

    def test_includes_manifest(self):
        zf = self._build_and_open()
        names = self._names(zf)
        manifest_files = [n for n in names if n.endswith('manifest.json')]
        self.assertEqual(len(manifest_files), 1)
        manifest = json.loads(zf.read(manifest_files[0]).decode('utf-8'))
        self.assertEqual(manifest['app_name'], 'Cardomancer')
        self.assertEqual(manifest['bundle_version'], 1)
        self.assertIn('generated_at', manifest)

    def test_includes_app_state(self):
        zf = self._build_and_open()
        names = self._names(zf)
        state_files = [n for n in names if n.endswith('app_state.json')]
        self.assertEqual(len(state_files), 1)
        state = json.loads(zf.read(state_files[0]).decode('utf-8'))
        self.assertIn('system', state)
        self.assertIn('worker', state)
        self.assertIn('hardware', state)
        self.assertEqual(state['worker']['state'], 'idle')

    def test_includes_logs(self):
        zf = self._build_and_open()
        names = self._names(zf)
        log_files = [n for n in names if '/logs/card_sorter.log' in n]
        self.assertEqual(len(log_files), 1)
        body = zf.read(log_files[0]).decode('utf-8')
        self.assertIn('this is a test log line', body)

    def test_includes_scan_session_metadata(self):
        zf = self._build_and_open()
        names = self._names(zf)
        sess_files = [n for n in names
                      if 'scan_logs/session_20260507_120000/session.json' in n]
        self.assertEqual(len(sess_files), 1)

    def test_excludes_frames_directory(self):
        zf = self._build_and_open()
        names = self._names(zf)
        frame_files = [n for n in names if '/frames/' in n]
        self.assertEqual(frame_files, [],
                         "frames/ directory must be excluded — bundle size")

    def test_excludes_collection_db(self):
        zf = self._build_and_open()
        names = self._names(zf)
        db_files = [n for n in names if n.endswith('collection.db')]
        self.assertEqual(db_files, [],
                         "collection.db is PII and must NOT be in bundles")

    def test_excludes_hash_db(self):
        zf = self._build_and_open()
        names = self._names(zf)
        hash_files = [n for n in names if 'card_hashes' in n]
        self.assertEqual(hash_files, [],
                         "Hash DBs are large and reproducible — exclude")

    def test_includes_known_project_files(self):
        zf = self._build_and_open()
        names = self._names(zf)
        last_setup = [n for n in names if n.endswith('_last_setup.json')]
        self.assertEqual(len(last_setup), 1)

    def test_includes_readme(self):
        zf = self._build_and_open()
        names = self._names(zf)
        readme = [n for n in names if n.endswith('README.txt')]
        self.assertEqual(len(readme), 1)
        body = zf.read(readme[0]).decode('utf-8')
        self.assertIn('Cardomancer', body)


if __name__ == '__main__':
    unittest.main()
