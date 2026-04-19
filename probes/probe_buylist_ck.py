# probes/probe_buylist_ck.py
# ---------------------------------------------------------------------------
# Probe CardKingdom's buylist landing page. The response is HTML, not JSON,
# so instead of the generic key-shape probe we parse with BeautifulSoup and
# assert a small set of selectors / marker strings stays present. If the
# page markup changes substantially, this fires and the Phase 3 scraper
# has to be re-tuned.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import time

import httpx
from bs4 import BeautifulSoup

from probes.base import (
    DEFAULT_TIMEOUT_S,
    USER_AGENT,
    ProbeResult,
    is_offline,
    snapshot_path,
)

SOURCE = "buylist_ck"
ENDPOINT = "https://www.cardkingdom.com/purchasing/mtg_singles"

# Substrings or CSS selectors the page must still contain for our scraper
# to have any hope of working. Keep the list short — these are the bones
# we rely on, not every button.
REQUIRED_MARKERS = [
    "Magic: the Gathering",
    "buylist",
]
REQUIRED_SELECTORS = [
    "title",
    "a[href*='/purchasing/']",
]


def probe() -> ProbeResult:
    start = time.time()
    warnings: list[str] = []
    if is_offline():
        return ProbeResult(
            source=SOURCE,
            ok=True,
            endpoint=ENDPOINT,
            duration_ms=int((time.time() - start) * 1000),
            warnings=["PROBES_OFFLINE=1 — network skipped."],
        )

    try:
        r = httpx.get(
            ENDPOINT,
            timeout=DEFAULT_TIMEOUT_S,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as e:
        return ProbeResult(
            source=SOURCE,
            ok=False,
            endpoint=ENDPOINT,
            duration_ms=int((time.time() - start) * 1000),
            warnings=[f"Fetch failed: {type(e).__name__}: {e}"],
        )

    html = r.text
    lowered = html.lower()
    missing_markers = [m for m in REQUIRED_MARKERS if m.lower() not in lowered]

    soup = BeautifulSoup(html, "html.parser")
    missing_selectors = [s for s in REQUIRED_SELECTORS if not soup.select(s)]

    diffs: list[str] = []
    for m in missing_markers:
        diffs.append(f"missing marker text: {m!r}")
    for s in missing_selectors:
        diffs.append(f"missing selector: {s!r}")

    # Snapshot the head of the HTML so drift is inspectable without dumping
    # a full page into the repo.
    snippet = html[:16 * 1024]
    ts_path = snapshot_path(SOURCE)
    try:
        ts_path.write_text(snippet, encoding="utf-8")
    except Exception:
        ts_path = None  # type: ignore[assignment]

    return ProbeResult(
        source=SOURCE,
        ok=not diffs,
        endpoint=ENDPOINT,
        duration_ms=int((time.time() - start) * 1000),
        shape_diff=diffs,
        snapshot_path=str(ts_path) if ts_path else None,
        warnings=warnings,
    )


if __name__ == "__main__":
    r = probe()
    print(r)
    sys.exit(0 if r.ok else 1)
