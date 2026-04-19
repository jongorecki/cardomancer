# tests/enrichment/test_scheduler.py
# ---------------------------------------------------------------------------
# RefreshScheduler smoke tests. Uses the stub sources so the probe body
# is a no-op; asserts registration, trigger flow, and emit plumbing.
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


class _CountingSource(EnrichmentSource):
    """Minimal EnrichmentSource for scheduler unit tests."""
    def __init__(self, name: str):
        self.name = name
        self.refresh_calls = 0
        self.done = threading.Event()

    def probe(self) -> bool:
        return True

    def refresh(self, emit=None, full=False):
        self.refresh_calls += 1
        if emit:
            emit("enrichment_refresh_progress",
                 {"source": self.name, "step": "done"})
        self.done.set()
        return RefreshResult(
            source=self.name, success=True, duration_ms=1,
            rows_changed=0, coverage_pct=0.0,
        )

    def coverage_report(self):
        return {"source": self.name}


class TestCronParse(unittest.TestCase):
    def test_valid_cron(self):
        t = _cron_to_trigger("0 3 * * 0")
        self.assertIsNotNone(t)

    def test_invalid_cron_raises(self):
        with self.assertRaises(ValueError):
            _cron_to_trigger("0 3 *")


class TestRefreshScheduler(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sched_test_")
        # Ensure any record_sync_attempt calls inside sources use an
        # isolated DB (defensive; not required for _CountingSource).
        self.db_path = os.path.join(self.tmpdir, "enrichment.db")
        conn = enrichment_db.get_connection(db_path=self.db_path)
        conn.close()

        self.events: list[tuple[str, dict]] = []

        def emit(e, d):
            self.events.append((e, d))

        self.scheduler = RefreshScheduler(emit=emit)

    def tearDown(self):
        try:
            self.scheduler.shutdown()
        finally:
            import shutil
            shutil.rmtree(self.tmpdir, ignore_errors=True)

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

    def test_manual_trigger_runs_refresh(self):
        src = _CountingSource("fake-manual")
        self.scheduler.register(src, cron="weekly")
        self.scheduler.trigger("fake-manual")

        src.done.wait(timeout=5.0)
        # Poll briefly for the completion event to flush.
        for _ in range(20):
            if any(e[0] == "enrichment_refresh_complete" for e in self.events):
                break
            time.sleep(0.05)

        self.assertEqual(src.refresh_calls, 1)
        event_names = [e[0] for e in self.events]
        self.assertIn("enrichment_refresh_started", event_names)
        self.assertIn("enrichment_refresh_complete", event_names)

    def test_trigger_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.scheduler.trigger("does-not-exist")


if __name__ == "__main__":
    unittest.main()
