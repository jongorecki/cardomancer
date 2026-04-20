# probes/probe_edhrec.py
# ---------------------------------------------------------------------------
# Probe json.edhrec.com commander page. Undocumented but widely used —
# field shape has shifted over time so a pinned snapshot is important.
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

SOURCE = "edhrec"
ENDPOINT = "https://json.edhrec.com/pages/commanders/atraxa-praetors-voice.json"

# Shape we rely on for staple / synergy extraction.
REQUIRED = [
    "container",
    "container.json_dict",
    "container.json_dict.cardlists",
    "container.json_dict.cardlists[].cardviews",
    "container.json_dict.cardlists[].cardviews[].name",
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
