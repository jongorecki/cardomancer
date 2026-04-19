# probes/probe_scryfall_search.py
# ---------------------------------------------------------------------------
# Probe Scryfall's public search API (/cards/search). Confirmed stable.
# Phase 0A scaffold; real shape assertion lands in Phase 0B-2.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import time

from probes.base import ProbeResult

SOURCE = "scryfall_search"
ENDPOINT = "https://api.scryfall.com/cards/search"
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
