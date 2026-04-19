# web_enrichment/base.py
# ---------------------------------------------------------------------------
# Abstract base contract for every external enrichment source
# (Scryfall Tagger, EDHREC, edhtop16, Spellbook, buylist vendors).
# Every concrete source lives in its own module and implements this.
# ---------------------------------------------------------------------------

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


EmitFn = Callable[[str, dict], None]


@dataclass
class RefreshResult:
    """Uniform result payload returned by EnrichmentSource.refresh()."""

    source: str
    success: bool
    duration_ms: int
    rows_changed: int = 0
    coverage_pct: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class EnrichmentSource(ABC):
    """Contract for every external data source.

    Concrete implementations live under web_enrichment/ (e.g. tagger.py,
    edhrec.py). Each source:

      - Declares its `name` (used as the key in sync_metadata / endpoints)
      - Implements probe() for fast health checks
      - Implements refresh() to pull data into enrichment.db
      - Implements coverage_report() for the /api/enrichment/sources card
    """

    name: str = ""

    @abstractmethod
    def probe(self) -> bool:
        """Fast health check. Returns True if the endpoint is reachable
        and the response shape matches the pinned snapshot.

        Must NOT mutate any data. Must complete in < 5s.
        """
        raise NotImplementedError

    @abstractmethod
    def refresh(self, emit: Optional[EmitFn] = None,
                full: bool = False) -> RefreshResult:
        """Pull latest data into enrichment.db.

        Args:
            emit: callback for progress events. Signature:
                  emit(event_name, data_dict). Emits
                  'enrichment_refresh_progress' with
                  {source, step, progress, total, message, ts}.
            full: if True, ignore incremental state and re-pull everything.

        Returns:
            RefreshResult summarising success/failure + coverage.

        Implementations MUST:
          - Use a transaction; never partial-commit on error.
          - Update sync_metadata on every call (success or failure).
          - Write coverage_reports rows for any per-key coverage claims.
          - Respect the source's rate limit.
          - Check probe() first; abort cleanly if probe fails.
        """
        raise NotImplementedError

    @abstractmethod
    def coverage_report(self) -> dict:
        """Current coverage state, read straight from enrichment.db.
        Must be fast (< 100ms) — no network calls here."""
        raise NotImplementedError
