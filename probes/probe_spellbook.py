# probes/probe_spellbook.py
# ---------------------------------------------------------------------------
# Probe backend.commanderspellbook.com — paginated combos REST endpoint.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys

import httpx

from probes.base import (
    DEFAULT_TIMEOUT_S,
    USER_AGENT,
    ProbeResult,
    run_http_probe,
)

SOURCE = "spellbook"
ENDPOINT = "https://backend.commanderspellbook.com/variants/"

REQUIRED = [
    "count",
    "results",
    "results[].id",
    "results[].uses",
    "results[].produces",
    "results[].identity",
]


def _fetch():
    r = httpx.get(
        ENDPOINT,
        params={"limit": 1},
        timeout=DEFAULT_TIMEOUT_S,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def probe() -> ProbeResult:
    return run_http_probe(SOURCE, ENDPOINT, _fetch, required_paths=REQUIRED)


if __name__ == "__main__":
    r = probe()
    print(r)
    sys.exit(0 if r.ok else 1)
