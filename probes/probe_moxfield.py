# probes/probe_moxfield.py
# ---------------------------------------------------------------------------
# Probe api2.moxfield.com. Unofficial, fragile; endpoint paths have
# changed before. Pin a known-stable public deck in Phase 0B-2.
# Phase 0A scaffold only.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import time

from probes.base import ProbeResult

SOURCE = "moxfield"
ENDPOINT = "https://api2.moxfield.com/v2/decks/all/"
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
