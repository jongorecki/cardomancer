# web_enrichment/stubs.py
# ---------------------------------------------------------------------------
# Placeholder EnrichmentSource implementations, one per planned source.
# Phase 0A registers these so the refresh scheduler, API endpoints, and
# /api/enrichment/sources card all work end-to-end. Phase 1+ agents
# replace each stub with a real implementation in its own module
# (web_enrichment/tagger.py, web_enrichment/edhrec.py, etc.).
#
# Every stub:
#   - has a correct `name`
#   - probe() returns True (trivially)
#   - refresh() records a sync_metadata row and returns a not-implemented
#     warning instead of raising; this keeps the server usable while
#     sources are still being built out
#   - coverage_report() reads sync_metadata only
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import time
from typing import Optional

import enrichment_db
from web_enrichment.base import EmitFn, EnrichmentSource, RefreshResult

logger = logging.getLogger(__name__)


class _StubSource(EnrichmentSource):
    """Base class for placeholder sources."""

    def __init__(self, name: str, message: str):
        self.name = name
        self._message = message

    def probe(self) -> bool:
        return True

    def refresh(self, emit: Optional[EmitFn] = None,
                full: bool = False) -> RefreshResult:
        start = time.time()
        warning = (f"{self.name}: not yet implemented (Phase 0A stub). "
                   f"{self._message}")
        logger.info("[stub] %s", warning)

        conn = enrichment_db.get_connection()
        try:
            enrichment_db.record_sync_attempt(
                conn,
                source=self.name,
                success=False,
                error="stub_not_implemented",
                coverage_pct=0.0,
            )
        finally:
            conn.close()

        duration_ms = int((time.time() - start) * 1000)
        return RefreshResult(
            source=self.name,
            success=False,
            duration_ms=duration_ms,
            rows_changed=0,
            coverage_pct=0.0,
            warnings=[warning],
        )

    def coverage_report(self) -> dict:
        conn = enrichment_db.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM sync_metadata WHERE source = ?",
                (self.name,),
            ).fetchone()
            return dict(row) if row else {
                "source": self.name,
                "last_success": None,
                "last_attempt": None,
                "error": None,
                "coverage_pct": 0.0,
            }
        finally:
            conn.close()


class TaggerStub(_StubSource):
    def __init__(self):
        super().__init__(
            "tagger",
            "Probe the GraphQL endpoint first; see probe_tagger.py.",
        )


class EDHRECStub(_StubSource):
    def __init__(self):
        super().__init__(
            "edhrec",
            "Pull commander + card pages from json.edhrec.com.",
        )


class EDHTop16Stub(_StubSource):
    def __init__(self):
        super().__init__(
            "edhtop16",
            "GraphQL query against edhtop16.com/api/graphql.",
        )


class SpellbookStub(_StubSource):
    def __init__(self):
        super().__init__(
            "spellbook",
            "Pull combos from backend.commanderspellbook.com.",
        )


class CardKingdomBuylistStub(_StubSource):
    def __init__(self):
        super().__init__(
            "buylist_ck",
            "Scrape CK buylist (daily).",
        )


class ScryfallPricesStub(_StubSource):
    def __init__(self):
        super().__init__(
            "prices",
            "Daily price refresh from Scryfall bulk.",
        )


ALL_STUBS: dict[str, type[_StubSource]] = {
    "tagger":     TaggerStub,
    "edhrec":     EDHRECStub,
    "edhtop16":   EDHTop16Stub,
    "spellbook":  SpellbookStub,
    "buylist_ck": CardKingdomBuylistStub,
    "prices":     ScryfallPricesStub,
}
