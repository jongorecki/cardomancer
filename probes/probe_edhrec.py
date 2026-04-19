# probes/probe_edhrec.py
# ---------------------------------------------------------------------------
# Probe json.edhrec.com commander + card pages. Undocumented but widely
# used; field shape has shifted over time so a pinned snapshot is key.
# Phase 0A scaffold only.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import time

from probes.base import ProbeResult

SOURCE = "edhrec"
ENDPOINT = "https://json.edhrec.com/pages/commanders/atraxa-praetors-voice.json"
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
