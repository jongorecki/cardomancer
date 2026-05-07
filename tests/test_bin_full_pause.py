"""
Regression tests for the bin-full-pause flow.

Per plans/autonomy_ladder.md: when a card's logical bin AND every bin in
its overflow chain are full, the machine pauses and prompts the user to
empty a bin instead of silently overflowing the same already-full bin.
The current card is dropped (in the least-bad full bin) and the *next*
cycle is the one that doesn't run.

These tests cover the chain-exhausted predicate and the pause + emit
flow. End-to-end "drop card → pause" testing happens via live preview
(no headless hardware available).
"""

import sys
import unittest
import unittest.mock as mock


def _install_hw_mocks():
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.ser = None
        sys.modules['gcode_control'] = gcode_mock


class ChainExhaustedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        import web_worker
        cls.worker_cls = web_worker.SortWorker

    def setUp(self):
        self.worker = self.worker_cls()
        # Capture emitted events for assertions
        self.emitted = []
        self.worker._emit_fn = lambda ev, data: self.emitted.append((ev, data))

    def test_no_overflow_no_full_bins_returns_false(self):
        self.worker.overflow_map = {}
        self.worker.bins_full = set()
        self.assertFalse(self.worker._is_chain_exhausted(3))

    def test_logical_bin_not_full_returns_false(self):
        """Default chain is just the logical bin itself; not full → not exhausted."""
        self.worker.overflow_map = {}
        self.worker.bins_full = set()
        self.assertFalse(self.worker._is_chain_exhausted(3))

    def test_logical_bin_full_no_overflow_returns_true(self):
        """Single-bin chain that's full → exhausted."""
        self.worker.overflow_map = {}
        self.worker.bins_full = {3}
        self.assertTrue(self.worker._is_chain_exhausted(3))

    def test_chain_with_some_free_returns_false(self):
        """Multi-bin chain where one is still free → NOT exhausted."""
        self.worker.overflow_map = {3: [3, 7, 9]}
        self.worker.bins_full = {3, 7}  # 9 still free
        self.assertFalse(self.worker._is_chain_exhausted(3))

    def test_full_chain_returns_true(self):
        """Every bin in chain marked full → exhausted."""
        self.worker.overflow_map = {3: [3, 7, 9]}
        self.worker.bins_full = {3, 7, 9}
        self.assertTrue(self.worker._is_chain_exhausted(3))

    def test_none_logical_bin_returns_false(self):
        """Defensive: missing logical_bin should not crash; treated as not exhausted."""
        self.assertFalse(self.worker._is_chain_exhausted(None))


class PauseForBinFullTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_hw_mocks()
        import web_worker
        cls.worker_cls = web_worker.SortWorker

    def setUp(self):
        self.worker = self.worker_cls()
        self.emitted = []
        self.worker._emit_fn = lambda ev, data: self.emitted.append((ev, data))
        # Pre-set state to 'sorting' so the transition is observable
        self.worker._state = 'sorting'
        self.worker.continuous_sorting = True

    def _last_event(self, name):
        for ev, data in reversed(self.emitted):
            if ev == name:
                return data
        return None

    def test_pause_flips_state_to_paused(self):
        self.worker.overflow_map = {3: [3, 7]}
        self.worker.bins_full = {3, 7}
        self.worker._pause_for_bin_full(3, physical_bin=7)
        self.assertEqual(self.worker._state, 'paused')

    def test_pause_emits_bin_full_prompt_with_chain(self):
        self.worker.overflow_map = {3: [3, 7, 9]}
        self.worker.bins_full = {3, 7, 9}
        self.worker._pause_for_bin_full(3, physical_bin=9)
        ev = self._last_event('bin_full_prompt')
        self.assertIsNotNone(ev)
        self.assertEqual(ev['logical_bin'], 3)
        self.assertEqual(ev['chain'], [3, 7, 9])
        self.assertEqual(ev['last_dropped'], 9)
        self.assertEqual(ev['bins_full'], [3, 7, 9])

    def test_pause_does_not_force_continuous_off(self):
        """Per design (refined 2026-05-07): leave continuous_sorting True
        so the user's Resume click after emptying a bin re-arms the loop
        automatically. If they Resume without emptying, the next cycle
        will route, drop, and re-pause — that's correct feedback."""
        self.worker.continuous_sorting = True
        self.worker.overflow_map = {3: [3]}
        self.worker.bins_full = {3}
        self.worker._pause_for_bin_full(3, physical_bin=3)
        self.assertTrue(self.worker.continuous_sorting,
                        "continuous_sorting must NOT be forced off; the "
                        "Resume flow handles re-arming naturally.")

    def test_pause_with_no_overflow_uses_logical_bin_as_chain(self):
        """No explicit overflow_map → chain is just [logical_bin]."""
        self.worker.overflow_map = {}
        self.worker.bins_full = {5}
        self.worker._pause_for_bin_full(5, physical_bin=5)
        ev = self._last_event('bin_full_prompt')
        self.assertEqual(ev['chain'], [5])


if __name__ == '__main__':
    unittest.main()
