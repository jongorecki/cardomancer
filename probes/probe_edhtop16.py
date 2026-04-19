# probes/probe_edhtop16.py
# ---------------------------------------------------------------------------
# Probe edhtop16.com/api/graphql. Runs a minimal introspection query to
# prove the GraphQL endpoint is up and the schema exposes its queryType.
# Per-field queries land in Phase 1 once the Phase 1 agent picks names.
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

INTROSPECTION_QUERY = (
    "{ __schema { queryType { name fields { name } } } }"
)

REQUIRED = [
    "data",
    "data.__schema",
    "data.__schema.queryType",
    "data.__schema.queryType.name",
    "data.__schema.queryType.fields",
    "data.__schema.queryType.fields[].name",
]


def _fetch():
    r = httpx.post(
        ENDPOINT,
        json={"query": INTROSPECTION_QUERY},
        timeout=DEFAULT_TIMEOUT_S,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
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
