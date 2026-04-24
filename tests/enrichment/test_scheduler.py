# tests/enrichment/test_scheduler.py
# ---------------------------------------------------------------------------
# RefreshScheduler tests. Covers:
#   - Cron registration + shorthand expansion
#   - Manual trigger mechanics and lockout
#   - Cron-path concurrency guard (skip if already running)
#   - Progress / started / complete event shapes
#   - Fake-source end-to-end (emit sequence + payload validation)
#   - Idempotency (two refreshes leave identical last_result)
#   - Error containment (source.refresh() raises, scheduler survives)
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import enrichment_db
from web_enrichment.base import EnrichmentSource, RefreshResult
from web_enrichment.scheduler import RefreshScheduler, _cron_to_trigger


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class _CountingSource(EnrichmentSource):
    """Minimal source for scheduler unit tests. No DB interaction."""

    def __init__(self, name: str):
        self.name = name
        self.refresh_calls = 0
        self.done = threading.Event()

    def probe(self) -> bool:
        return True

    def refresh(self, emit=None, full=False):
        self.refresh_calls += 1
        if emit:
            emit("enrichment_refresh_progress", {
                "source": self.name,
                "step": 1,
                "total": 1,
                "message": "done",
                "ts": "2026-01-01T00:00:00",
            })
        self.done.set()
        return RefreshResult(
            source=self.name, success=True, duration_ms=1,
            rows_changed=0, coverage_pct=0.0,
        )

    def coverage_report(self):
        return {"source": self.name}


class _SlowSource(EnrichmentSource):
    """Source that blocks until released, for concurrency tests."""

    def __init__(self, name: str):
        self.name = name
        self.refresh_calls = 0
        self._start_event = threading.Event()
        self._release_event = threading.Event()
        self.done = threading.Event()

    def probe(self) -> bool:
        return True

    def refresh(self, emit=None, full=False):
        self.refresh_calls += 1
        self._start_event.set()      # signal: refresh is now running
        self._release_event.wait()   # block until test releases
        self.done.set()
        return RefreshResult(
            source=self.name, success=True, duration_ms=10,
            rows_changed=0, coverage_pct=0.0,
        )

    def coverage_report(self):
        return {}

    def wait_started(self, timeout: float = 2.0) -> bool:
        return self._start_event.wait(timeout)

    def release(self):
        self._release_event.set()


class _ErrorSource(EnrichmentSource):
    """Source whose refresh() always raises."""

    def __init__(self, name: str):
        self.name = name
        self.done = threading.Event()

    def probe(self) -> bool:
        return True

    def refresh(self, emit=None, full=False):
        self.done.set()
        raise RuntimeError("intentional test error")

    def coverage_report(self):
        return {}


class _ProgressSource(EnrichmentSource):
    """Source that emits multiple progress events with varying payloads."""

    STEPS = [
        {"step": 0, "total": 3, "message": "probe"},
        {"step": 1, "total": 3, "message": "fetch"},
        {"step": 2, "total": 3, "message": "write"},
        {"step": 3, "total": 3, "message": "done"},
    ]

    def __init__(self, name: str = "progress_src"):
        self.name = name
        self.done = threading.Event()

    def probe(self) -> bool:
        return True

    def refresh(self, emit=None, full=False):
        ts = "2026-01-01T03:00:00"
        for step in self.STEPS:
            if emit:
                emit("enrichment_refresh_progress", {
                    "source": self.name,
                    "step": step["step"],
                    "total": step["total"],
                    "message": step["message"],
                    "ts": ts,
                })
        self.done.set()
        return RefreshResult(
            source=self.name, success=True, duration_ms=5,
            rows_changed=3, coverage_pct=100.0,
        )

    def coverage_report(self):
        return {}


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def _make_scheduler():
    events: list[tuple[str, dict]] = []

    def emit(e, d):
        events.append((e, d))

    sched = RefreshScheduler(emit=emit)
    return sched, events


def _wait_for_event(events: list, event_name: str, timeout: float = 5.0) -> bool:
    """Poll until an event with the given name appears, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(e[0] == event_name for e in events):
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# Cron parsing tests
# ---------------------------------------------------------------------------

class TestCronParse(unittest.TestCase):

    def test_valid_cron(self):
        t = _cron_to_trigger("0 3 * * 0")
        self.assertIsNotNone(t)

    def test_invalid_cron_raises(self):
        with self.assertRaises(ValueError):
            _cron_to_trigger("0 3 *")

    def test_five_field_expansion(self):
        t = _cron_to_trigger("15 4 * * 1")
        self.assertIsNotNone(t)


# ---------------------------------------------------------------------------
# Registration and listing tests
# ---------------------------------------------------------------------------

class TestCronRegistration(unittest.TestCase):

    def setUp(self):
        self.scheduler, self.events = _make_scheduler()

    def tearDown(self):
        try:
            self.scheduler.shutdown()
        except Exception:
            pass

    def test_register_and_list(self):
        src = _CountingSource("fake")
        self.scheduler.register(src, cron="weekly")
        entries = self.scheduler.list_sources()
        names = {e["name"] for e in entries}
        self.assertIn("fake", names)
        entry = next(e for e in entries if e["name"] == "fake")
        self.assertEqual(entry["cron"], "0 3 * * 0")

    def test_shorthand_daily(self):
        src = _CountingSource("fake-daily")
        self.scheduler.register(src, cron="daily")
        entry = next(e for e in self.scheduler.list_sources()
                     if e["name"] == "fake-daily")
        self.assertEqual(entry["cron"], "0 2 * * *")

    def test_shorthand_weekly_with_known_name(self):
        for name, expected_cron in [
            ("tagger",    "0 3 * * 0"),
            ("edhrec",    "0 4 * * 0"),
            ("edhtop16",  "0 5 * * 0"),
            ("spellbook", "0 6 * * 0"),
        ]:
            src = _CountingSource(name)
            self.scheduler.register(src, cron="weekly")
            entry = next(e for e in self.scheduler.list_sources()
                         if e["name"] == name)
            self.assertEqual(entry["cron"], expected_cron,
                             f"Wrong cron for {name}")

    def test_unregister_removes_source(self):
        src = _CountingSource("removable")
        self.scheduler.register(src, cron="daily")
        self.assertIn("removable", {e["name"] for e in self.scheduler.list_sources()})
        self.scheduler.unregister("removable")
        self.assertNotIn("removable", {e["name"] for e in self.scheduler.list_sources()})

    def test_list_running_flag_false_initially(self):
        src = _CountingSource("not-running")
        self.scheduler.register(src, cron="daily")
        entry = next(e for e in self.scheduler.list_sources()
                     if e["name"] == "not-running")
        self.assertFalse(entry["running"])

    def test_list_last_result_none_before_run(self):
        src = _CountingSource("fresh")
        self.scheduler.register(src, cron="daily")
        entry = next(e for e in self.scheduler.list_sources()
                     if e["name"] == "fresh")
        self.assertIsNone(entry["last_result"])


# ---------------------------------------------------------------------------
# Manual trigger tests
# ---------------------------------------------------------------------------

class TestManualTrigger(unittest.TestCase):

    def setUp(self):
        self.scheduler, self.events = _make_scheduler()

    def tearDown(self):
        try:
            self.scheduler.shutdown()
        except Exception:
            pass

    def test_trigger_runs_refresh(self):
        src = _CountingSource("fake-manual")
        self.scheduler.register(src, cron="weekly")
        self.scheduler.trigger("fake-manual")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        self.assertEqual(src.refresh_calls, 1)

    def test_trigger_emits_started_event(self):
        src = _CountingSource("t-started")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("t-started")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_started")
        started = [e for e in self.events if e[0] == "enrichment_refresh_started"]
        self.assertTrue(len(started) >= 1)

    def test_trigger_emits_complete_event(self):
        src = _CountingSource("t-complete")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("t-complete")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        complete = [e for e in self.events if e[0] == "enrichment_refresh_complete"]
        self.assertTrue(len(complete) >= 1)

    def test_trigger_unknown_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.scheduler.trigger("does-not-exist")

    def test_trigger_sets_running_flag(self):
        src = _SlowSource("running-check")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("running-check")
        src.wait_started(timeout=2.0)
        entry = next(e for e in self.scheduler.list_sources()
                     if e["name"] == "running-check")
        self.assertTrue(entry["running"])
        src.release()
        src.done.wait(timeout=5.0)

    def test_trigger_clears_running_flag_after_completion(self):
        src = _CountingSource("flag-clear")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("flag-clear")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        time.sleep(0.1)  # brief settle
        entry = next(e for e in self.scheduler.list_sources()
                     if e["name"] == "flag-clear")
        self.assertFalse(entry["running"])

    def test_trigger_updates_last_result(self):
        src = _CountingSource("last-result")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("last-result")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        time.sleep(0.1)
        entry = next(e for e in self.scheduler.list_sources()
                     if e["name"] == "last-result")
        self.assertIsNotNone(entry["last_result"])
        self.assertTrue(entry["last_result"]["success"])


# ---------------------------------------------------------------------------
# Concurrent-refresh lockout tests
# ---------------------------------------------------------------------------

class TestConcurrencyLockout(unittest.TestCase):
    """The scheduler MUST prevent two concurrent refreshes of the same source."""

    def setUp(self):
        self.scheduler, self.events = _make_scheduler()

    def tearDown(self):
        try:
            self.scheduler.shutdown()
        except Exception:
            pass

    def test_double_trigger_raises_runtime_error(self):
        """trigger() while same source is in-flight must raise RuntimeError."""
        src = _SlowSource("lock-test")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("lock-test")
        src.wait_started(timeout=2.0)  # first refresh is now running

        try:
            with self.assertRaises(RuntimeError) as cm:
                self.scheduler.trigger("lock-test")
            self.assertIn("already in progress", str(cm.exception))
        finally:
            src.release()
            src.done.wait(timeout=5.0)

    def test_cron_path_skips_if_already_running(self):
        """Direct _run_source (cron path) must skip if source is in _running."""
        src = _SlowSource("cron-skip")
        self.scheduler.register(src, cron="daily")

        # Start a manual trigger (occupies the running slot)
        self.scheduler.trigger("cron-skip")
        src.wait_started(timeout=2.0)

        # Simulate a cron fire: call _run_source directly
        self.scheduler._run_source("cron-skip")

        src.release()
        src.done.wait(timeout=5.0)

        # Only ONE refresh should have actually run
        self.assertEqual(src.refresh_calls, 1)

    def test_sequential_triggers_both_run(self):
        """After first completes, a second trigger must succeed."""
        src = _CountingSource("seq-test")
        self.scheduler.register(src, cron="daily")

        # First run
        src.done.clear()
        self.scheduler.trigger("seq-test")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        time.sleep(0.1)

        # Second run
        src.done.clear()
        self.events.clear()
        self.scheduler.trigger("seq-test")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")

        self.assertEqual(src.refresh_calls, 2)

    def test_different_sources_run_concurrently(self):
        """Two different sources must be allowed to run simultaneously."""
        src_a = _SlowSource("concurrent-a")
        src_b = _SlowSource("concurrent-b")
        self.scheduler.register(src_a, cron="daily")
        self.scheduler.register(src_b, cron="daily")

        self.scheduler.trigger("concurrent-a")
        self.scheduler.trigger("concurrent-b")

        started_a = src_a.wait_started(timeout=2.0)
        started_b = src_b.wait_started(timeout=2.0)
        self.assertTrue(started_a, "source-a never started")
        self.assertTrue(started_b, "source-b never started")

        src_a.release()
        src_b.release()
        src_a.done.wait(timeout=5.0)
        src_b.done.wait(timeout=5.0)


# ---------------------------------------------------------------------------
# Event shape validation tests
# ---------------------------------------------------------------------------

class TestEventShapes(unittest.TestCase):
    """Verify that emitted events carry the exact fields app.js consumes."""

    STARTED_REQUIRED = {"source", "manual", "full", "ts"}
    PROGRESS_REQUIRED = {"source", "step", "total", "message", "ts"}
    COMPLETE_REQUIRED = {"source", "success", "duration_ms", "rows_changed",
                         "coverage_pct", "errors", "warnings", "ts"}

    def setUp(self):
        self.scheduler, self.events = _make_scheduler()

    def tearDown(self):
        try:
            self.scheduler.shutdown()
        except Exception:
            pass

    def _run_and_collect(self, src: EnrichmentSource, timeout: float = 5.0):
        """Register, trigger, wait for completion, return all events."""
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger(src.name)
        if hasattr(src, "done"):
            src.done.wait(timeout=timeout)
        _wait_for_event(self.events, "enrichment_refresh_complete", timeout=timeout)
        time.sleep(0.05)
        return list(self.events)

    def test_started_event_has_required_fields(self):
        src = _CountingSource("shape-started")
        events = self._run_and_collect(src)
        started = [e[1] for e in events if e[0] == "enrichment_refresh_started"]
        self.assertTrue(len(started) >= 1, "No enrichment_refresh_started event")
        payload = started[0]
        missing = self.STARTED_REQUIRED - set(payload.keys())
        self.assertFalse(missing, f"started payload missing fields: {missing}")

    def test_started_event_source_matches(self):
        src = _CountingSource("shape-src-name")
        events = self._run_and_collect(src)
        started = [e[1] for e in events if e[0] == "enrichment_refresh_started"]
        self.assertEqual(started[0]["source"], "shape-src-name")

    def test_started_event_manual_true_for_trigger(self):
        src = _CountingSource("manual-flag")
        events = self._run_and_collect(src)
        started = [e[1] for e in events if e[0] == "enrichment_refresh_started"]
        self.assertTrue(started[0]["manual"])

    def test_progress_event_has_required_fields(self):
        src = _ProgressSource("shape-progress")
        events = self._run_and_collect(src)
        progress_evts = [e[1] for e in events
                         if e[0] == "enrichment_refresh_progress"]
        self.assertTrue(len(progress_evts) >= 1, "No enrichment_refresh_progress events")
        for payload in progress_evts:
            missing = self.PROGRESS_REQUIRED - set(payload.keys())
            self.assertFalse(missing,
                             f"progress payload missing fields: {missing}")

    def test_progress_step_and_total_are_ints(self):
        src = _ProgressSource("int-check")
        events = self._run_and_collect(src)
        progress_evts = [e[1] for e in events
                         if e[0] == "enrichment_refresh_progress"]
        for payload in progress_evts:
            self.assertIsInstance(payload["step"], int)
            self.assertIsInstance(payload["total"], int)

    def test_progress_message_is_string(self):
        src = _ProgressSource("msg-check")
        events = self._run_and_collect(src)
        progress_evts = [e[1] for e in events
                         if e[0] == "enrichment_refresh_progress"]
        for payload in progress_evts:
            self.assertIsInstance(payload["message"], str)

    def test_complete_event_has_required_fields(self):
        src = _CountingSource("shape-complete")
        events = self._run_and_collect(src)
        complete = [e[1] for e in events if e[0] == "enrichment_refresh_complete"]
        self.assertTrue(len(complete) >= 1, "No enrichment_refresh_complete event")
        payload = complete[0]
        missing = self.COMPLETE_REQUIRED - set(payload.keys())
        self.assertFalse(missing, f"complete payload missing fields: {missing}")

    def test_complete_event_types(self):
        src = _CountingSource("complete-types")
        events = self._run_and_collect(src)
        complete = [e[1] for e in events if e[0] == "enrichment_refresh_complete"]
        payload = complete[0]
        self.assertIsInstance(payload["success"], bool)
        self.assertIsInstance(payload["duration_ms"], int)
        self.assertIsInstance(payload["rows_changed"], int)
        self.assertIsInstance(payload["coverage_pct"], float)
        self.assertIsInstance(payload["errors"], list)
        self.assertIsInstance(payload["warnings"], list)
        self.assertIsInstance(payload["ts"], str)

    def test_complete_event_source_matches(self):
        src = _CountingSource("complete-src")
        events = self._run_and_collect(src)
        complete = [e[1] for e in events if e[0] == "enrichment_refresh_complete"]
        self.assertEqual(complete[0]["source"], "complete-src")


# ---------------------------------------------------------------------------
# Fake-source end-to-end tests
# ---------------------------------------------------------------------------

class TestEndToEnd(unittest.TestCase):
    """Full refresh cycle: events in correct order, payload coherent."""

    def setUp(self):
        self.scheduler, self.events = _make_scheduler()

    def tearDown(self):
        try:
            self.scheduler.shutdown()
        except Exception:
            pass

    def test_event_order_started_progress_complete(self):
        """Events must appear in order: started → (progress*) → complete."""
        src = _ProgressSource("e2e-order")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("e2e-order")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        time.sleep(0.05)

        names = [e[0] for e in self.events]
        self.assertIn("enrichment_refresh_started", names)
        self.assertIn("enrichment_refresh_progress", names)
        self.assertIn("enrichment_refresh_complete", names)

        first = names.index("enrichment_refresh_started")
        last = len(names) - 1 - names[::-1].index("enrichment_refresh_complete")
        self.assertLess(first, last,
                        "started must come before complete in event stream")

    def test_progress_events_count(self):
        """_ProgressSource emits exactly len(STEPS) progress events."""
        src = _ProgressSource("e2e-count")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("e2e-count")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        time.sleep(0.05)

        progress_evts = [e for e in self.events
                         if e[0] == "enrichment_refresh_progress"]
        self.assertEqual(len(progress_evts), len(_ProgressSource.STEPS))

    def test_complete_success_true_on_healthy_source(self):
        src = _CountingSource("e2e-success")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("e2e-success")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        complete = [e[1] for e in self.events
                    if e[0] == "enrichment_refresh_complete"]
        self.assertTrue(complete[0]["success"])

    def test_complete_has_empty_errors_on_success(self):
        src = _CountingSource("e2e-no-errors")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("e2e-no-errors")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        complete = [e[1] for e in self.events
                    if e[0] == "enrichment_refresh_complete"]
        self.assertEqual(complete[0]["errors"], [])

    def test_error_source_complete_event_success_false(self):
        """If source.refresh() raises, complete event must have success=False."""
        src = _ErrorSource("e2e-error")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("e2e-error")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        complete = [e[1] for e in self.events
                    if e[0] == "enrichment_refresh_complete"]
        self.assertTrue(len(complete) >= 1, "No complete event after error")
        self.assertFalse(complete[0]["success"])

    def test_error_source_errors_list_non_empty(self):
        src = _ErrorSource("e2e-error-list")
        self.scheduler.register(src, cron="daily")
        self.scheduler.trigger("e2e-error-list")
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        complete = [e[1] for e in self.events
                    if e[0] == "enrichment_refresh_complete"]
        self.assertTrue(len(complete[0]["errors"]) > 0)

    def test_error_source_does_not_prevent_future_trigger(self):
        """After a failed refresh, the source can be triggered again."""
        src = _CountingSource("recover")
        # Override to fail first time
        _orig_refresh = src.refresh
        call_count = [0]

        def _fail_first(emit=None, full=False):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first call fails")
            return _orig_refresh(emit=emit, full=full)

        src.refresh = _fail_first
        self.scheduler.register(src, cron="daily")

        # First trigger — will fail
        src.done.clear()
        self.scheduler.trigger("recover")
        _wait_for_event(self.events, "enrichment_refresh_complete")
        time.sleep(0.1)

        # Second trigger — must succeed, no lockout
        src.done.clear()
        self.events.clear()
        self.scheduler.trigger("recover")  # should NOT raise RuntimeError
        src.done.wait(timeout=5.0)
        _wait_for_event(self.events, "enrichment_refresh_complete")
        complete = [e[1] for e in self.events
                    if e[0] == "enrichment_refresh_complete"]
        self.assertTrue(complete[0]["success"])


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------

class TestIdempotency(unittest.TestCase):
    """Two sequential refreshes must leave identical last_result state."""

    def setUp(self):
        self.scheduler, self.events = _make_scheduler()

    def tearDown(self):
        try:
            self.scheduler.shutdown()
        except Exception:
            pass

    def test_two_refreshes_identical_success_state(self):
        src = _CountingSource("idem-test")
        self.scheduler.register(src, cron="daily")

        def _do_one_refresh():
            src.done.clear()
            self.events.clear()
            self.scheduler.trigger("idem-test")
            src.done.wait(timeout=5.0)
            _wait_for_event(self.events, "enrichment_refresh_complete")
            time.sleep(0.05)

        _do_one_refresh()
        result1 = next(e for e in self.scheduler.list_sources()
                       if e["name"] == "idem-test")["last_result"]

        _do_one_refresh()
        result2 = next(e for e in self.scheduler.list_sources()
                       if e["name"] == "idem-test")["last_result"]

        # Both runs must report same structural state
        self.assertEqual(result1["success"], result2["success"])
        self.assertEqual(result1["source"], result2["source"])
        self.assertEqual(result1["rows_changed"], result2["rows_changed"])
        self.assertEqual(result1["errors"], result2["errors"])

    def test_refresh_call_count_increments_properly(self):
        src = _CountingSource("call-count")
        self.scheduler.register(src, cron="daily")

        for i in range(3):
            src.done.clear()
            self.events.clear()
            self.scheduler.trigger("call-count")
            src.done.wait(timeout=5.0)
            _wait_for_event(self.events, "enrichment_refresh_complete")
            time.sleep(0.05)
            self.assertEqual(src.refresh_calls, i + 1)


# ---------------------------------------------------------------------------
# No-emit (None) robustness
# ---------------------------------------------------------------------------

class TestNoEmit(unittest.TestCase):
    """Scheduler with emit=None must still work end-to-end without crashing."""

    def test_trigger_works_without_emit_callback(self):
        sched = RefreshScheduler(emit=None)
        src = _CountingSource("no-emit")
        sched.register(src, cron="daily")
        sched.trigger("no-emit")
        src.done.wait(timeout=5.0)
        self.assertEqual(src.refresh_calls, 1)
        try:
            sched.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
