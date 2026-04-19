# probes/probe_scryfall_bulk.py
# ---------------------------------------------------------------------------
# Probe the Scryfall bulk-data catalog (/bulk-data). Confirmed public API.
# The probe catches response-shape drift; it does NOT download any bulk file.
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

SOURCE = "scryfall_bulk"
ENDPOINT = "https://api.scryfall.com/bulk-data"

# Shape contract — keys Scryfall guarantees on the catalog response.
REQUIRED = [
    "object",
    "has_more",
    "data",
    "data[].id",
    "data[].type",
    "data[].updated_at",
    "data[].download_uri",
    "data[].size",
]


def _fetch():
    r = httpx.get(
        ENDPOINT,
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
