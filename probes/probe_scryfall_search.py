# probes/probe_scryfall_search.py
# ---------------------------------------------------------------------------
# Probe the Scryfall /cards/search API. Confirmed public API.
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

SOURCE = "scryfall_search"
ENDPOINT = "https://api.scryfall.com/cards/search"
QUERY = "otag:removal"  # stable, always has results

REQUIRED = [
    "object",
    "total_cards",
    "has_more",
    "data",
    "data[].id",
    "data[].oracle_id",
    "data[].name",
    "data[].type_line",
    "data[].color_identity",
    "data[].legalities",
]


def _fetch():
    r = httpx.get(
        ENDPOINT,
        params={"q": QUERY, "page": 1},
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
