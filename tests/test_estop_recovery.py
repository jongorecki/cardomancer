"""
Regression tests for E-stop -> Reset & Re-home recovery.

Bug background (was open from 2026-04-11 to 2026-05-07): after pressing
E-stop mid-continuous-sort and then clicking "Reset & Re-home", the
machine cleared the halt and re-homed, but the sort session did not
resume. Root cause: emergency_stop() forces continuous_sorting=False
to prevent auto-resume the instant state flips, but
_cmd_reset_after_estop() never restored it from the pre-estop snapshot.
After Resume, state went to 'sorting' but continuous_sorting=False so
no detect_and_sort cycle was enqueued — the machine sat silent while
the (out-of-sync) Continuous button still visually said "on."

These tests pin the contract: the pre_estop_state snapshot's
'continuous' flag MUST be restored on reset for sessions that survived
the E-stop, and MUST NOT be restored when no session was active.

Tests do not touch real hardware — gcode_control is mocked at module
level so the M999 / re-home / serial paths short-circuit, and we
verify only the state-machine fields the bug touched.
"""

import sys
import unittest
import unittest.mock as mock


def _install_hw_mocks():
    """Replace gcode_control with a mock so importing web_worker doesn't
    require a real serial port or Marlin. Idempotent."""
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.ser = None
        gcode_mock.Z_MAX = 100.0
        gcode_mock.X_SOURCE_BIN = 547.1
        gcode_mock.home_all = lambda: None
        gcode_mock.home_x = lambda: None
        gcode_mock.home_z = lambda: None
        gcode_mock.get_bin_locations = lambda: {}
        gcode_mock.invalidate_probe_cache_for_x = lambda x: None
        sys.modules['gcode_control'] = gcode_mock


class FakeTracker:
    """Minimal stand-in for the real ScanTracker — _cmd_reset_after_estop
    only checks `self.tracker is not None`, never calls methods on it."""


class EstopRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        # Import after mocks are installed — web_worker does
        # `import gcode_control` at module top.
        import web_worker
        cls.web_worker = web_worker

    def _make_worker(self):
        return self.web_worker.SortWorker()

    def test_reset_restores_continuous_when_session_survived(self):
        """Mid-continuous E-stop -> Reset should land in 'paused' AND
        restore continuous_sorting=True from the pre-estop snapshot."""
        w = self._make_worker()
        w.tracker = FakeTracker()
        # Simulate the bookkeeping emergency_stop() did:
        w._pre_estop_state = {
            'had_session': True,
            'previous_state': 'sorting',
            'sort_mode': 'custom_file',
            'continuous': True,
        }
        w.continuous_sorting = False  # forced off by emergency_stop
        w._state = 'estopped'

        try:
            w._cmd_reset_after_estop()
        except Exception:
            # Hardware paths may raise after the state-restoration step.
            # That's fine — the fields we care about are set first.
            pass

        self.assertEqual(w._state, 'paused',
                         "Reset with surviving session should land in 'paused'")
        self.assertTrue(w.continuous_sorting,
                        "continuous_sorting must be restored from "
                        "_pre_estop_state['continuous']=True; otherwise "
                        "Resume produces a silent machine.")

    def test_reset_does_not_enable_continuous_when_no_session(self):
        """Reset with no active session should land in 'idle' and leave
        continuous_sorting alone (False) — there's nothing to resume."""
        w = self._make_worker()
        w.tracker = None
        w._pre_estop_state = {
            'had_session': False,
            'previous_state': 'idle',
            'sort_mode': None,
            'continuous': False,
        }
        w.continuous_sorting = False
        w._state = 'estopped'

        try:
            w._cmd_reset_after_estop()
        except Exception:
            pass

        self.assertEqual(w._state, 'idle',
                         "Reset with no session should land in 'idle'")
        self.assertFalse(w.continuous_sorting,
                         "continuous_sorting must remain False when no "
                         "session was active.")

    def test_reset_clears_abort_flag(self):
        """The abort flag emergency_stop() sets must be cleared on reset
        so the next detect_and_sort doesn't bail at its first abort
        check (the original symptom was 'pressing Detect does nothing')."""
        w = self._make_worker()
        w.tracker = FakeTracker()
        w._pre_estop_state = {
            'had_session': True, 'previous_state': 'sorting',
            'sort_mode': 'custom_file', 'continuous': True,
        }
        w._abort_requested = True  # set by emergency_stop()
        w._state = 'estopped'

        try:
            w._cmd_reset_after_estop()
        except Exception:
            pass

        self.assertFalse(w._abort_requested,
                         "_abort_requested must be cleared on reset, or "
                         "the next detect_and_sort early-outs and the "
                         "session looks frozen.")

    def test_reset_clears_pre_estop_snapshot(self):
        """The snapshot must be cleared after reset so a subsequent
        E-stop starts fresh (otherwise stale data leaks across cycles)."""
        w = self._make_worker()
        w.tracker = FakeTracker()
        w._pre_estop_state = {
            'had_session': True, 'previous_state': 'sorting',
            'sort_mode': 'custom_file', 'continuous': True,
        }
        w._state = 'estopped'

        try:
            w._cmd_reset_after_estop()
        except Exception:
            pass

        self.assertIsNone(w._pre_estop_state,
                          "_pre_estop_state must be None after reset.")


if __name__ == '__main__':
    unittest.main()
