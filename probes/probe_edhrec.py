# probes/probe_edhrec.py
# ---------------------------------------------------------------------------
# Probe json.edhrec.com pages. Undocumented but widely used —
# field shape has shifted over time so a pinned snapshot is important.
#
# Checks:
#   1. Commander page (primary — pinned snapshot source)
#   2. Color top-cards page (archetype data)
#   3. Card detail page (salt + inclusion rate, Phase 1.10)
#   4. Theme page (theme membership data, Phase 1.10)
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys

import httpx

from probes.base import (
    DEFAULT_TIMEOUT_S,
    USER_AGENT,
    ProbeResult,
    require_keys,
    run_http_probe,
)

SOURCE = "edhrec"
# Primary endpoint: commander page (well-known, stable)
ENDPOINT = "https://json.edhrec.com/pages/commanders/atraxa-praetors-voice.json"
# Secondary endpoints — non-fatal if unavailable (403 is common on EDHREC)
COLOR_ENDPOINT = "https://json.edhrec.com/pages/top/white.json"
CARD_ENDPOINT = "https://json.edhrec.com/pages/cards/sol-ring.json"
THEME_ENDPOINT = "https://json.edhrec.com/pages/themes/lifegain.json"

# Shape we rely on for staple / synergy extraction.
# Commander and color/theme pages share the cardlists structure.
REQUIRED = [
    "container",
    "container.json_dict",
    "container.json_dict.cardlists",
    "container.json_dict.cardlists[].cardviews",
    "container.json_dict.cardlists[].cardviews[].name",
]

# Required fields on a card detail page (for salt + inclusion rate)
CARD_PAGE_REQUIRED = [
    "container",
    "container.json_dict",
    "container.json_dict.card",
    "container.json_dict.card.name",
    "container.json_dict.card.num_decks",
    "container.json_dict.card.potential_decks",
]


def _fetch():
    r = httpx.get(
        ENDPOINT,
        timeout=DEFAULT_TIMEOUT_S,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def _fetch_secondary(url: str) -> dict:
    r = httpx.get(
        url,
        timeout=DEFAULT_TIMEOUT_S,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def probe() -> ProbeResult:
    """Probe commander page (primary) + spot checks on color/card/theme pages.

    The commander page is the pinned-snapshot source.  Color, card, and theme
    page probes are non-fatal — a 403 or shape warning is appended but the
    probe still passes (EDHREC returns 403 on some pages; the source gracefully
    skips them at refresh time).
    """
    from probes.base import is_offline
    # Primary: pin against commander page
    result = run_http_probe(SOURCE, ENDPOINT, _fetch, required_paths=REQUIRED)
    if not result.ok:
        return result

    # Secondary checks (all non-fatal); skip in offline mode
    if not is_offline():
        _spot_check(result, "color top-cards", COLOR_ENDPOINT, REQUIRED)
        _spot_check(result, "card detail (salt)", CARD_ENDPOINT, CARD_PAGE_REQUIRED)
        _spot_check(result, "theme page", THEME_ENDPOINT, REQUIRED)

    return result


def _spot_check(
    result: ProbeResult,
    label: str,
    url: str,
    required: list[str],
) -> None:
    """Non-fatal secondary probe: adds a warning on failure, never sets ok=False."""
    try:
        data = _fetch_secondary(url)
        missing = require_keys(data, required)
        if missing:
            result.warnings.append(
                f"{label} shape drift (non-fatal): missing {missing}")
    except Exception as exc:
        result.warnings.append(
            f"{label} probe skipped (non-fatal fetch error: {exc})")


if __name__ == "__main__":
    r = probe()
    print(r)
    sys.exit(0 if r.ok else 1)
