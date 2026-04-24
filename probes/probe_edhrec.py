# probes/probe_edhrec.py
# ---------------------------------------------------------------------------
# Probe json.edhrec.com pages. Undocumented but widely used —
# field shape has shifted over time so a pinned snapshot is important.
#
# Checks both a commander page (legacy) and a color top-cards page (new
# archetype source).  Both share the same cardlists structure.
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
# Primary endpoint: commander page (well-known, stable)
ENDPOINT = "https://json.edhrec.com/pages/commanders/atraxa-praetors-voice.json"
# Secondary endpoint: color top-cards page (new archetype source as of 2026-04-23)
COLOR_ENDPOINT = "https://json.edhrec.com/pages/top/white.json"

# Shape we rely on for staple / synergy extraction.
# Both commander pages and color pages share this structure.
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


def _fetch_color():
    r = httpx.get(
        COLOR_ENDPOINT,
        timeout=DEFAULT_TIMEOUT_S,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def probe() -> ProbeResult:
    """Probe commander page (primary) + color page (secondary).

    The commander page is the pinned-snapshot source.  The color page probe
    checks that the new archetype data source is reachable and shape-matches.
    If the color page is unavailable, a warning is added but the probe still
    passes (403 is common on pages EDHREC hasn't indexed).
    """
    # Primary: pin against commander page
    result = run_http_probe(SOURCE, ENDPOINT, _fetch, required_paths=REQUIRED)
    if not result.ok:
        return result

    # Secondary: verify color page shape (non-fatal if unavailable)
    try:
        color_data = _fetch_color()
        from probes.base import require_keys
        missing = require_keys(color_data, REQUIRED)
        if missing:
            result.warnings.append(
                f"Color page shape drift: missing {missing}")
    except Exception as exc:
        result.warnings.append(
            f"Color page probe skipped (fetch error: {exc})")

    return result


if __name__ == "__main__":
    r = probe()
    print(r)
    sys.exit(0 if r.ok else 1)
