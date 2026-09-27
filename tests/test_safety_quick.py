"""
Regression tests for the 2026-09 safety fixes (TASK-001, TASK-006).

- _auto_safety_reset must LIFT Z (Z=0 is fully down on this machine).
- The abort flag must not survive resume / new session / continuous
  start, or every card cycle bails at its first _abort_check().

gcode_control is mocked at module level; no hardware is touched.
"""

import sys
import unittest
import unittest.mock as mock


def _install_hw_mocks():
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.ser = None
        gcode_mock.Z_MAX = 100.0
        gcode_mock.X_SOURCE_BIN = 547.1
        gcode_mock.get_bin_locations = lambda: {}
        gcode_mock.invalidate_probe_cache_for_x = lambda x: None
        sys.modules['gcode_control'] = gcode_mock


class SafetyResetLiftsZTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        import web_worker
        cls.web_worker = web_worker

    def test_safety_reset_moves_z_to_clear_height_not_zero(self):
        gc = sys.modules['gcode_control']
        with mock.patch.object(gc, 'is_connected', return_value=True), \
             mock.patch.object(gc, 'Z_CLEAR_HEIGHT', 200.0, create=True), \
             mock.patch.object(gc, 'move_z') as move_z, \
             mock.patch.object(gc, '_send_gcode'):
            w = self.web_worker.SortWorker()
            w._auto_safety_reset('detect_and_sort', RuntimeError('boom'))
        move_z.assert_called_once_with(200.0)
        for call in move_z.call_args_list:
            self.assertNotEqual(call.args[0], 0)


class AbortFlagClearedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        import web_worker
        cls.web_worker = web_worker

    def test_resume_clears_abort_left_by_camera_autopause(self):
        w = self.web_worker.SortWorker()
        w._state = 'paused'
        w.request_abort('camera freeze')
        w._cmd_resume()
        self.assertFalse(w._abort_check())
        self.assertEqual(w.state, 'sorting')

    def test_start_continuous_clears_abort(self):
        w = self.web_worker.SortWorker()
        w._state = 'sorting'
        w.request_abort('stray cancel')
        with mock.patch.object(w, 'enqueue'):
            w._cmd_start_continuous()
        self.assertFalse(w._abort_check())


if __name__ == '__main__':
    unittest.main()
