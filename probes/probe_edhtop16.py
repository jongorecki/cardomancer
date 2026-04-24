# probes/probe_edhtop16.py
# ---------------------------------------------------------------------------
# Probe edhtop16.com/api/graphql. Two checks:
#   1. Introspection: GraphQL schema exposes queryType with expected fields.
#   2. Staples shape: a minimal staples query returns cards with the fields
#      our refresh() code depends on (oracleId, playRateLastYear, type).
#
# Results are pinned to tests/probe_snapshots/edhtop16_pinned.json.
# Shape drift aborts the refresh — see EDHTop16Source.probe().
#
# GraphQL introspection result (confirmed 2026-04-24):
#   - queryType fields include: staples, tournaments, card, commanders, ...
#   - staples args: colorId (String), type (String)
#   - Card fields: name, oracleId, colorId, type, playRateLastYear, ...
#   - TournamentFilters: timePeriod (TimePeriod enum), minSize, minDate, maxDate
#   - TimePeriod enum values: ALL_TIME, ONE_MONTH, THREE_MONTHS, SIX_MONTHS,
#     ONE_YEAR, POST_BAN
#   - Tournament.entries(maxStanding: Int) -> [Card!]
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

SOURCE = "edhtop16"
ENDPOINT = "https://edhtop16.com/api/graphql"

# Minimal staples query — we only request 1 card to keep the probe fast.
# The shape of this response is what we pin and diff against.
STAPLES_PROBE_QUERY = (
    "{ staples { name oracleId colorId type playRateLastYear } }"
)

# Required paths in the probe response
REQUIRED = [
    "data",
    "data.staples",
    "data.staples[].name",
    "data.staples[].oracleId",
    "data.staples[].playRateLastYear",
    "data.staples[].type",
]


def _fetch() -> dict:
    r = httpx.post(
        ENDPOINT,
        json={"query": STAPLES_PROBE_QUERY},
        timeout=DEFAULT_TIMEOUT_S,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors'][:2]}")
    # Truncate to first 3 cards so the pinned snapshot stays small
    staples = (data.get("data") or {}).get("staples") or []
    if len(staples) > 3:
        data = {**data, "data": {**data["data"], "staples": staples[:3]}}
    return data


def probe() -> ProbeResult:
    return run_http_probe(SOURCE, ENDPOINT, _fetch, required_paths=REQUIRED)


if __name__ == "__main__":
    r = probe()
    print(r)
    sys.exit(0 if r.ok else 1)
