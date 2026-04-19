# probes/probe_scryfall_bulk.py
# ---------------------------------------------------------------------------
# Probe the Scryfall bulk-data catalog (/bulk-data). Confirmed stable
# public API; probe mainly catches response-shape drift over time.
#
# Phase 0A scaffold: probe body is a no-op stub. Phase 0B-2 fills in the
# real HTTP call + shape assertion + pinned snapshot.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import time

from probes.base import ProbeResult

SOURCE = "scryfall_bulk"
ENDPOINT = "https://api.scryfall.com/bulk-data"
IMPLEMENTED = False


def probe() -> ProbeResult:
    start = time.time()
    warnings = []
    if not IMPLEMENTED:
        warnings.append("Probe stub — real shape assertion lands in Phase 0B.")
    return ProbeResult(
        source=SOURCE,
        ok=True,
        endpoint=ENDPOINT,
        duration_ms=int((time.time() - start) * 1000),
        warnings=warnings,
    )


if __name__ == "__main__":
    r = probe()
    print(r)
    sys.exit(0 if r.ok else 1)
