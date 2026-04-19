# probes/probe_edhtop16.py
# ---------------------------------------------------------------------------
# Probe edhtop16.com/api/graphql. Run introspection first (Phase 0B-2)
# to lock down the schema, then pin a known-good query response.
# Phase 0A scaffold only.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import time

from probes.base import ProbeResult

SOURCE = "edhtop16"
ENDPOINT = "https://edhtop16.com/api/graphql"
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
