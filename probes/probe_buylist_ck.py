# probes/probe_buylist_ck.py
# ---------------------------------------------------------------------------
# Probe CardKingdom's buylist page. HTML, not JSON — Phase 0B-2 decides
# on parser (BeautifulSoup). Phase 0A scaffold only.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import time

from probes.base import ProbeResult

SOURCE = "buylist_ck"
ENDPOINT = "https://www.cardkingdom.com/purchasing/mtg_singles"
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
