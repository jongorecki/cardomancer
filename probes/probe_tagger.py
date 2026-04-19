# probes/probe_tagger.py
# ---------------------------------------------------------------------------
# Probe the Scryfall Tagger GraphQL endpoint. Undocumented and known to
# require session cookies / CSRF tokens; see plans/web_enrichment_source_probes.md §3.
#
# Strategy: send the standard GraphQL introspection query as an
# unauthenticated POST. A successful response (200 + `data.__schema`)
# means the endpoint is reachable without auth and we can build on it.
# A 401/403/empty body is the documented-fallback signal — we report OK
# with a warning so run_all.py still succeeds, but flag that Phase 1
# needs the search-API fallback path described in the planning doc.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
import time

import httpx

from probes.base import (
    DEFAULT_TIMEOUT_S,
    USER_AGENT,
    ProbeResult,
    is_offline,
    snapshot_path,
    load_pinned,
    write_snapshot,
    compare_keys,
)

SOURCE = "tagger"
ENDPOINT = "https://tagger.scryfall.com/graphql"
INTROSPECTION_QUERY = (
    "{ __schema { queryType { name } types { name kind } } }"
)


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
    except Exception as e:
        return ProbeResult(
            source=SOURCE,
            ok=True,
            endpoint=ENDPOINT,
            duration_ms=int((time.time() - start) * 1000),
            warnings=[
                f"Tagger unreachable ({type(e).__name__}: {e}).",
                "Phase 1 must use the Scryfall search-API fallback.",
            ],
        )

    if r.status_code in (401, 403):
        return ProbeResult(
            source=SOURCE,
            ok=True,
            endpoint=ENDPOINT,
            duration_ms=int((time.time() - start) * 1000),
            warnings=[
                f"Tagger returned {r.status_code} — auth required.",
                "Phase 1 must implement session-cookie login or fall back.",
            ],
        )

    try:
        data = r.json()
    except Exception as e:
        return ProbeResult(
            source=SOURCE,
            ok=False,
            endpoint=ENDPOINT,
            duration_ms=int((time.time() - start) * 1000),
            warnings=[f"Non-JSON response ({type(e).__name__}: {e})."],
        )

    if not isinstance(data, dict) or "data" not in data:
        return ProbeResult(
            source=SOURCE,
            ok=True,
            endpoint=ENDPOINT,
            duration_ms=int((time.time() - start) * 1000),
            warnings=[
                "Tagger responded without `data`; introspection may be disabled.",
                f"Body sample: {str(data)[:200]!r}",
            ],
        )

    pinned = load_pinned(SOURCE)
    pinned_file = snapshot_path(SOURCE, pinned=True)
    if pinned is None:
        write_snapshot(pinned_file, data)
        warnings.append("Pinned initial Tagger snapshot on first successful probe.")
        return ProbeResult(
            source=SOURCE,
            ok=True,
            endpoint=ENDPOINT,
            duration_ms=int((time.time() - start) * 1000),
            pinned_path=str(pinned_file),
            warnings=warnings,
        )

    diffs = compare_keys(pinned, data)
    ts_path = snapshot_path(SOURCE)
    try:
        write_snapshot(ts_path, data)
    except Exception:
        ts_path = None  # type: ignore[assignment]
    return ProbeResult(
        source=SOURCE,
        ok=not diffs,
        endpoint=ENDPOINT,
        duration_ms=int((time.time() - start) * 1000),
        shape_diff=diffs,
        snapshot_path=str(ts_path) if ts_path else None,
        pinned_path=str(pinned_file),
        warnings=warnings,
    )


if __name__ == "__main__":
    r = probe()
    print(r)
    sys.exit(0 if r.ok else 1)
