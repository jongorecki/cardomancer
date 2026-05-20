"""
Contract test for the logger.exception() cleanup pass.

Several runtime modules now call `logger.exception(...)` inside their
`except` blocks instead of `logger.error(f"...{e}")`. The whole point
is that the resulting log entry contains the full traceback rather
than just the str(exception) — that traceback is what the support
bundle ships to whoever's helping diagnose a problem.

These tests pin the contract:
  1. logger.exception(...) inside an active except: writes a
     "Traceback (most recent call last):" block to the configured
     handler, and the exception type+message survive into the log.
  2. The traceback survives all the way into the support bundle
     (`logs/card_sorter.log` member of the zip).
  3. logger.warning(..., exc_info=True) — the pattern used at two
     sites where the failure is recoverable but the cause is still
     worth capturing — also produces a traceback.

If anyone "simplifies" these sites back to `logger.error(f"... {e}")`,
test #1 should fail and surface the regression before it ships.
"""

import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
import zipfile


class LoggerExceptionContractTests(unittest.TestCase):
    """Verify logger.exception captures traceback content."""

    def setUp(self):
        # Isolate so we don't leak handlers into other test files.
        self.log_path = tempfile.NamedTemporaryFile(
            mode='w', delete=False, suffix='.log', encoding='utf-8'
        )
        self.log_path.close()
        self.handler = logging.FileHandler(self.log_path.name, encoding='utf-8')
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(
            logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
        )
        self.logger = logging.getLogger(f'test_logger_exception_{id(self)}')
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        self.logger.propagate = False

    def tearDown(self):
        self.handler.close()
        self.logger.removeHandler(self.handler)
        try:
            os.unlink(self.log_path.name)
        except OSError:
            pass

    def _read_log(self):
        with open(self.log_path.name, 'r', encoding='utf-8') as f:
            return f.read()

    def test_exception_captures_traceback(self):
        """logger.exception() inside an except block writes traceback."""
        try:
            raise ValueError("simulated upstream failure")
        except Exception:
            self.logger.exception("operation failed")

        content = self._read_log()
        # The message survives
        self.assertIn("operation failed", content)
        # The exception type + message survive
        self.assertIn("ValueError", content)
        self.assertIn("simulated upstream failure", content)
        # The traceback header is present — this is the load-bearing
        # difference between logger.error and logger.exception
        self.assertIn("Traceback (most recent call last)", content)
        # Level is ERROR (logger.exception() always logs at ERROR)
        self.assertIn("[ERROR]", content)

    def test_warning_with_exc_info_captures_traceback(self):
        """logger.warning(..., exc_info=True) also captures traceback.

        Used at two sites (collection/filter eval error, stale-session
        check on connect) where the failure is recoverable but the
        cause is still worth seeing.
        """
        try:
            raise RuntimeError("recoverable miss")
        except Exception:
            self.logger.warning("ignored failure", exc_info=True)

        content = self._read_log()
        self.assertIn("ignored failure", content)
        self.assertIn("RuntimeError", content)
        self.assertIn("recoverable miss", content)
        self.assertIn("Traceback (most recent call last)", content)
        # Level stays WARNING — this differs from .exception()
        self.assertIn("[WARNING]", content)

    def test_error_without_exc_info_does_not_capture_traceback(self):
        """Sanity: bare logger.error(f"... {e}") does NOT capture a
        traceback. This pins the regression the pass was about:
        the old pattern silently dropped the stack."""
        try:
            raise ValueError("upstream")
        except Exception as e:
            self.logger.error(f"operation failed: {e}")

        content = self._read_log()
        self.assertIn("operation failed", content)
        self.assertIn("upstream", content)  # message body has it
        # But no traceback — that's the point
        self.assertNotIn("Traceback (most recent call last)", content)


def _install_hw_mocks():
    """Mock gcode_control so support_bundle can import without hardware."""
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.ser = None
        gcode_mock.SERIAL_PORT = 'COM3'
        gcode_mock.X_SOURCE_BIN = 547.1
        gcode_mock.X_STAGING_POSITION = 427.9
        gcode_mock.CAMERA_X_OFFSET = 100.0
        gcode_mock.get_bin_locations = lambda: {}
        gcode_mock._last_serial_error = None
        sys.modules['gcode_control'] = gcode_mock


class _FakeWorker:
    """Minimal worker stand-in matching test_support_bundle.py."""
    def __init__(self):
        self._state = 'idle'
        self.tracker = None
        self.continuous_sorting = False
        self.scan_count = 0
        self.bins_full = set()
        self.bin_card_counts = {}
        self.overflow_map = {}


class LoggerExceptionInSupportBundleTests(unittest.TestCase):
    """End-to-end: a traceback written by logger.exception ends up
    inside the support-bundle zip's logs/card_sorter.log member."""

    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        import support_bundle
        cls.sb = support_bundle

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='cm_logex_test_')
        os.makedirs(os.path.join(self.repo, 'logs'))
        os.makedirs(os.path.join(self.repo, 'scan_logs'))
        # Synthesize a log file that mimics what logger.exception
        # produces in production (with a real traceback).
        self.log_path = os.path.join(self.repo, 'logs', 'card_sorter.log')
        test_logger = logging.getLogger(f'cm_logex_{id(self)}')
        test_logger.handlers = []
        handler = logging.FileHandler(self.log_path, encoding='utf-8')
        handler.setFormatter(
            logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
        )
        test_logger.setLevel(logging.DEBUG)
        test_logger.addHandler(handler)
        test_logger.propagate = False
        try:
            raise OSError("disk read failed at sector 42")
        except Exception:
            test_logger.exception("safety-reset failed")
        handler.close()
        test_logger.removeHandler(handler)

        # Files the bundle expects to find
        with open(os.path.join(self.repo, '_last_setup.json'),
                  'w', encoding='utf-8') as f:
            f.write('{"dest": [], "source": []}')

        self.worker = _FakeWorker()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_traceback_in_bundle(self):
        data = self.sb.build_support_bundle(
            repo_root=self.repo, worker=self.worker, app_name='Cardomancer'
        )
        zf = zipfile.ZipFile(io.BytesIO(data))
        log_members = [n for n in zf.namelist()
                       if '/logs/card_sorter.log' in n]
        self.assertEqual(len(log_members), 1,
                         "support bundle should include the rotating log file")
        body = zf.read(log_members[0]).decode('utf-8')
        # The original message
        self.assertIn("safety-reset failed", body)
        # The exception type + message
        self.assertIn("OSError", body)
        self.assertIn("disk read failed at sector 42", body)
        # The traceback header — the whole point of this pass
        self.assertIn("Traceback (most recent call last)", body)


if __name__ == '__main__':
    unittest.main()
