# probes/probe_moxfield.py
# ---------------------------------------------------------------------------
# Probe api2.moxfield.com. Unofficial and fragile — the v2 path changed
# once before; a pinned snapshot is how we'll catch the next drift.
#
# The probe targets a single public deck. The deck ID is configurable via
# the MOXFIELD_PROBE_DECK_ID env var; if unset, a placeholder from the
# planning docs is used. Operators should pick a long-lived public deck
# they trust and pin their own snapshot on first successful run.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import sys

import httpx

from probes.base import (
    DEFAULT_TIMEOUT_S,
    USER_AGENT,
    ProbeResult,
    run_http_probe,
)

SOURCE = "moxfield"
# Placeholder deck ID — override via MOXFIELD_PROBE_DECK_ID.
_DEFAULT_DECK_ID = "lzbasAFQhEqY5x5SmJRZ9w"
DECK_ID = os.environ.get("MOXFIELD_PROBE_DECK_ID", _DEFAULT_DECK_ID)
ENDPOINT = f"https://api2.moxfield.com/v2/decks/all/{DECK_ID}"

REQUIRED = [
    "id",
    "name",
    "format",
    "mainboard",
]


def _fetch():
    r = httpx.get(
        ENDPOINT,
        timeout=DEFAULT_TIMEOUT_S,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    r.raise_for_status()
    return r.json()


def probe() -> ProbeResult:
    return run_http_probe(SOURCE, ENDPOINT, _fetch, required_paths=REQUIRED)


if __name__ == "__main__":
    r = probe()
    print(r)
    sys.exit(0 if r.ok else 1)
