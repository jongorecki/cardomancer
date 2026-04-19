# web_enrichment/scheduler.py
# ---------------------------------------------------------------------------
# Wraps APScheduler's BackgroundScheduler with source registration and
# emit-based progress reporting. One instance lives on the Flask app;
# every EnrichmentSource registers a cron entry here.
#
# Cron strings:
#   - 'weekly' and 'daily' are shorthands expanded to sensible defaults
#     (Sunday 03:00 UTC staggered by source; daily 02:00 UTC).
#   - Any valid crontab expression (5 fields) is also accepted.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from web_enrichment.base import EnrichmentSource, RefreshResult

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, dict], None]


# Default cron windows per source. Weekly jobs run Sunday UTC, staggered
# so they don't all pound their endpoints in the same minute.
_WEEKLY_DEFAULTS = {
    "tagger":    "0 3 * * 0",
    "edhrec":    "0 4 * * 0",
    "edhtop16":  "0 5 * * 0",
    "spellbook": "0 6 * * 0",
}
_DAILY_DEFAULT = "0 2 * * *"  # 02:00 UTC


def _expand_cron(cron: str, source_name: str) -> str:
    """Translate 'weekly' / 'daily' shorthands into real crontab strings."""
    if cron == "weekly":
        return _WEEKLY_DEFAULTS.get(source_name, "0 3 * * 0")
    if cron == "daily":
        return _DAILY_DEFAULT
    return cron


def _cron_to_trigger(cron_expr: str) -> CronTrigger:
    """Build a CronTrigger from a standard 5-field crontab string."""
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"Cron expression must have 5 fields: {cron_expr!r}")
    minute, hour, day, month, dow = parts
    return CronTrigger(minute=minute, hour=hour, day=day,
                       month=month, day_of_week=dow)


class RefreshScheduler:
    """In-process cron scheduler for enrichment refreshes.

    Usage in web_server.py:
        scheduler = RefreshScheduler(emit=lambda e, d: socketio.emit(e, d))
        scheduler.register(TaggerSource(), cron="weekly")
        scheduler.start()
        atexit.register(scheduler.shutdown)
    """

    def __init__(self, emit: Optional[EmitFn] = None):
        self._sched = BackgroundScheduler(timezone="UTC")
        self._sources: dict[str, EnrichmentSource] = {}
        self._crons: dict[str, str] = {}
        self._last_results: dict[str, RefreshResult] = {}
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._emit = emit

    # -- Registration -------------------------------------------------------

    def register(self, source: EnrichmentSource, cron: str) -> None:
        """Register a source with a cron expression. `cron` may be the
        shorthand 'weekly' / 'daily' or a 5-field crontab string."""
        if not source.name:
            raise ValueError("EnrichmentSource.name must be set")

        expanded = _expand_cron(cron, source.name)
        trigger = _cron_to_trigger(expanded)

        self._sources[source.name] = source
        self._crons[source.name] = expanded

        self._sched.add_job(
            func=self._run_source,
            trigger=trigger,
            args=(source.name,),
            id=f"refresh-{source.name}",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info("Registered enrichment source '%s' cron=%s",
                    source.name, expanded)

    def unregister(self, source_name: str) -> None:
        if source_name in self._sources:
            try:
                self._sched.remove_job(f"refresh-{source_name}")
            except Exception:
                pass
            self._sources.pop(source_name, None)
            self._crons.pop(source_name, None)

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not self._sched.running:
            self._sched.start()
            logger.info("RefreshScheduler started")

    def shutdown(self) -> None:
        if self._sched.running:
            try:
                self._sched.shutdown(wait=False)
            except Exception as e:
                logger.warning("Scheduler shutdown error: %s", e)

    # -- Manual trigger -----------------------------------------------------

    def trigger(self, source_name: str, full: bool = False) -> None:
        """Manually trigger a refresh. Runs in a background thread so the
        HTTP handler returns immediately. Progress is emitted via
        `self._emit`."""
        if source_name not in self._sources:
            raise KeyError(f"Unknown source: {source_name}")

        with self._lock:
            if source_name in self._running:
                raise RuntimeError(
                    f"Refresh for '{source_name}' already in progress")
            self._running.add(source_name)

        t = threading.Thread(
            target=self._run_source,
            args=(source_name,),
            kwargs={"full": full, "manual": True},
            daemon=True,
            name=f"refresh-{source_name}",
        )
        t.start()

    # -- Introspection ------------------------------------------------------

    def list_sources(self) -> list[dict]:
        """Sources + their cron + last-run state + next run time."""
        out: list[dict] = []
        for name, src in self._sources.items():
            job = self._sched.get_job(f"refresh-{name}")
            nrt = getattr(job, "next_run_time", None) if job else None
            next_run = nrt.isoformat() if nrt else None
            last = self._last_results.get(name)
            out.append({
                "name": name,
                "cron": self._crons.get(name),
                "next_run": next_run,
                "running": name in self._running,
                "last_result": self._result_to_dict(last) if last else None,
            })
        return out

    # -- Internals ----------------------------------------------------------

    def _run_source(self, source_name: str, full: bool = False,
                    manual: bool = False) -> None:
        """Invoke a source's refresh() with error containment."""
        source = self._sources.get(source_name)
        if source is None:
            logger.error("RefreshScheduler: unknown source '%s'", source_name)
            return

        with self._lock:
            self._running.add(source_name)

        start_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._emit_event("enrichment_refresh_started", {
            "source": source_name,
            "manual": manual,
            "full": full,
            "ts": start_ts,
        })

        try:
            result = source.refresh(emit=self._emit, full=full)
        except Exception as e:
            logger.exception("Refresh failed for %s", source_name)
            result = RefreshResult(
                source=source_name,
                success=False,
                duration_ms=0,
                errors=[f"{type(e).__name__}: {e}"],
            )
        finally:
            with self._lock:
                self._running.discard(source_name)

        self._last_results[source_name] = result
        self._emit_event("enrichment_refresh_complete", {
            **self._result_to_dict(result),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    def _emit_event(self, event: str, data: dict) -> None:
        if self._emit is None:
            return
        try:
            self._emit(event, data)
        except Exception as e:
            logger.warning("Scheduler emit failed: %s", e)

    @staticmethod
    def _result_to_dict(r: RefreshResult) -> dict:
        return {
            "source": r.source,
            "success": r.success,
            "duration_ms": r.duration_ms,
            "rows_changed": r.rows_changed,
            "coverage_pct": r.coverage_pct,
            "errors": list(r.errors),
            "warnings": list(r.warnings),
        }
