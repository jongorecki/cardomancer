# probes/probe_tagger.py
# ---------------------------------------------------------------------------
# Probe the Scryfall Tagger GraphQL endpoint. UNDOCUMENTED: Phase 0B-2
# must perform network-tab reconnaissance first to identify operations
# and authentication requirements; see
# plans/web_enrichment_source_probes.md §3.
#
# Phase 0A scaffold only. IMPLEMENTED=False ⇒ probe() reports a warning
# and exits OK so run_all.py doesn't break the test suite.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import time

from probes.base import ProbeResult

SOURCE = "tagger"
ENDPOINT = "https://tagger.scryfall.com/graphql"
IMPLEMENTED = False


def probe() -> ProbeResult:
    start = time.time()
    warnings = [
        "Scryfall Tagger GraphQL endpoint is undocumented.",
        "Phase 0B-2 must pin a real snapshot before this probe asserts.",
    ]
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
