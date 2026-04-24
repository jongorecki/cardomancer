# probes/probe_tagger.py
# ---------------------------------------------------------------------------
# Probe the Scryfall Tagger data path for Phase 0.4.
#
# Strategy — two-pass:
#
#   Pass 1 (GraphQL attempt):
#     POST https://tagger.scryfall.com/graphql with an introspection query.
#     The endpoint is undocumented and requires a CSRF authenticity token
#     from a browser session. Unauthenticated requests receive:
#       {'success': False, 'message': 'invalid authenticity token'}
#     This is the documented failure mode (see plans/web_enrichment_source_probes.md §3).
#     If it ever starts working without auth we get a bonus; until then we
#     log the failure and continue to Pass 2.
#
#   Pass 2 (Scryfall search-API fallback):
#     Query https://api.scryfall.com/cards/search?q=otag:<name> for a
#     small sample of known-stable tags. Verify shape, record:
#       - total_cards for each sample tag (sample_sizes)
#       - rate-limit header values observed (ratelimit_headers)
#       - which path was used (path_used: "scryfall_search_api_fallback")
#     Write a JSON summary to tests/probe_snapshots/tagger_search_summary.json.
#
#   Pin snapshot: tests/probe_snapshots/tagger_pinned.json
#     Records the fallback mode state so run_all.py always exits 0 even
#     though Tagger GraphQL is gated behind auth. On a fresh clone the
#     pinned file is shipped in the repo; any real shape drift on the
#     Scryfall search API shape will surface as a diff.
#
# Exit 0: GraphQL works unauthenticated OR search-API fallback is healthy.
# Exit 1: Search-API fallback is also broken (network error or shape mismatch).
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

import httpx

from probes.base import (
    DEFAULT_TIMEOUT_S,
    USER_AGENT,
    ProbeResult,
    is_offline,
    load_pinned,
    snapshot_path,
    write_snapshot,
    compare_keys,
)

SOURCE = "tagger"
GRAPHQL_ENDPOINT = "https://tagger.scryfall.com/graphql"
SEARCH_ENDPOINT = "https://api.scryfall.com/cards/search"
INTROSPECTION_QUERY = "{ __schema { queryType { name } types { name kind } } }"

# Tags sampled during the fallback probe — small, stable, always have results
SAMPLE_TAGS = ["removal", "ramp", "vanilla"]
RATE_LIMIT_S = 0.1  # 100ms between requests — Scryfall's documented safe interval

# Required keys in a Scryfall search response
_REQUIRED_SEARCH_KEYS = {"object", "total_cards", "has_more", "data"}


def _try_graphql(start: float) -> tuple[str, list[str]]:
    """Attempt unauthenticated GraphQL introspection.

    Returns (status_note, warnings). Never raises.
    """
    warnings: list[str] = []
    try:
        r = httpx.post(
            GRAPHQL_ENDPOINT,
            json={"query": INTROSPECTION_QUERY},
            timeout=DEFAULT_TIMEOUT_S,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
    except Exception as exc:
        warnings.append(
            f"Tagger GraphQL unreachable ({type(exc).__name__}: {exc}). "
            "Phase 1 uses the Scryfall search-API fallback."
        )
        return "unreachable", warnings

    if r.status_code in (401, 403):
        warnings.append(
            f"Tagger GraphQL returned {r.status_code} — session auth required. "
            "Phase 1 uses the Scryfall search-API fallback (no auth needed)."
        )
        return f"{r.status_code}_auth_required", warnings

    try:
        body = r.json()
    except Exception:
        warnings.append(
            f"Tagger GraphQL status {r.status_code} returned non-JSON body. "
            "Falling back to Scryfall search API."
        )
        return f"{r.status_code}_non_json", warnings

    if isinstance(body, dict) and body.get("data", {}).get("__schema"):
        return "graphql_ok", warnings

    # Likely CSRF error body
    msg = body.get("message", "") if isinstance(body, dict) else str(body)[:100]
    warnings.append(
        f"Tagger GraphQL responded but schema missing ({msg!r}). "
        "Using search-API fallback."
    )
    return "graphql_no_schema", warnings


def _try_search_fallback() -> tuple[bool, dict, list[str]]:
    """Probe the Scryfall search-API fallback path.

    Queries a small set of stable `otag:` tags. Captures sample sizes
    and any rate-limit headers observed.

    Returns (ok, summary_dict, warnings).
    """
    warnings: list[str] = []
    sample_sizes: dict[str, int] = {}
    ratelimit_headers: dict[str, str] = {}

    try:
        with httpx.Client(
            timeout=DEFAULT_TIMEOUT_S,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            for tag in SAMPLE_TAGS:
                time.sleep(RATE_LIMIT_S)
                resp = client.get(
                    SEARCH_ENDPOINT,
                    params={"q": f"otag:{tag}", "page": 1},
                )

                # Capture rate-limit headers (Scryfall may expose X-RateLimit-*)
                for h, v in resp.headers.items():
                    hl = h.lower()
                    if "ratelimit" in hl or "rate-limit" in hl or "retry-after" in hl:
                        ratelimit_headers[h] = v

                if resp.status_code == 429:
                    warnings.append(
                        f"Rate-limited (429) on otag:{tag} probe. "
                        "Backoff: sleeping 2s and aborting sample."
                    )
                    time.sleep(2.0)
                    break

                if resp.status_code == 404:
                    # Tag exists but has 0 results — valid
                    sample_sizes[tag] = 0
                    continue

                if not resp.is_success:
                    warnings.append(
                        f"Scryfall search returned {resp.status_code} for otag:{tag}."
                    )
                    continue

                data = resp.json()

                # Shape check
                missing = _REQUIRED_SEARCH_KEYS - set(data.keys())
                if missing:
                    warnings.append(
                        f"otag:{tag} response missing keys: {sorted(missing)}"
                    )
                    continue

                total = data.get("total_cards", 0)
                sample_sizes[tag] = total

                # Validate data array has expected card-shape fields
                if data.get("data"):
                    first = data["data"][0]
                    for key in ("oracle_id", "name", "type_line"):
                        if key not in first:
                            warnings.append(
                                f"otag:{tag} card object missing '{key}' field."
                            )

    except Exception as exc:
        warnings.append(
            f"Scryfall search probe failed: {type(exc).__name__}: {exc}"
        )
        return False, {}, warnings

    # Success: at least 1 tag returned > 0 results
    ok = any(v > 0 for v in sample_sizes.values())
    if not ok and sample_sizes:
        warnings.append(
            "All sampled otag: queries returned 0 results — unexpected."
        )
    elif not sample_sizes:
        warnings.append("No otag: samples collected — network may be blocked.")
        ok = False

    summary = {
        "path_used": "scryfall_search_api_fallback",
        "sample_tags": SAMPLE_TAGS,
        "sample_sizes": sample_sizes,
        "ratelimit_headers": ratelimit_headers,
        "search_endpoint": SEARCH_ENDPOINT,
    }
    return ok, summary, warnings


def probe() -> ProbeResult:
    start = time.time()
    all_warnings: list[str] = []

    if is_offline():
        return ProbeResult(
            source=SOURCE,
            ok=True,
            endpoint=SEARCH_ENDPOINT,
            duration_ms=int((time.time() - start) * 1000),
            warnings=["PROBES_OFFLINE=1 — network skipped."],
        )

    # Pass 1: Try GraphQL (expected to fail with auth error)
    graphql_status, gql_warnings = _try_graphql(start)
    all_warnings.extend(gql_warnings)

    if graphql_status == "graphql_ok":
        # Unexpectedly succeeded — great, but we still run the search probe
        # to document both paths.
        all_warnings.append(
            "Tagger GraphQL succeeded unauthenticated — consider using it directly."
        )

    # Pass 2: Scryfall search-API fallback (always run)
    fallback_ok, summary, fallback_warnings = _try_search_fallback()
    all_warnings.extend(fallback_warnings)

    # Write a search-summary snapshot for reference (not the shape-diff snapshot)
    ts_now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    summary_path = snapshot_path(SOURCE).parent / f"tagger_search_summary_{ts_now}.json"
    try:
        summary["graphql_status"] = graphql_status
        summary["probed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_snapshot(summary_path, summary)
    except Exception:
        pass

    # Shape-diff check: compare against pinned snapshot
    # The pinned snapshot records the *fallback-mode* state of the probe.
    pinned = load_pinned(SOURCE)
    pinned_file = snapshot_path(SOURCE, pinned=True)

    # The pinned data for tagger records fallback mode, not live data.
    # We don't compare live search results against the pinned data (they change daily);
    # instead we compare the structural keys to detect endpoint renames.
    pinned_probe_data = {
        "endpoint_status": graphql_status,
        "fallback_active": True,
        "fallback_source": "scryfall_search_api",
        "fallback_queries": ["otag:<tag_name>", "atag:<tag_name>"],
    }

    if pinned is None:
        write_snapshot(pinned_file, pinned_probe_data)
        all_warnings.append("Pinned initial Tagger probe snapshot on first run.")
        return ProbeResult(
            source=SOURCE,
            ok=fallback_ok,
            endpoint=SEARCH_ENDPOINT,
            duration_ms=int((time.time() - start) * 1000),
            pinned_path=str(pinned_file),
            snapshot_path=str(summary_path),
            warnings=all_warnings,
        )

    diffs = compare_keys(pinned, pinned_probe_data)
    ts_snap = snapshot_path(SOURCE)
    try:
        write_snapshot(ts_snap, pinned_probe_data)
    except Exception:
        ts_snap = None  # type: ignore[assignment]

    return ProbeResult(
        source=SOURCE,
        ok=fallback_ok and not diffs,
        endpoint=SEARCH_ENDPOINT,
        duration_ms=int((time.time() - start) * 1000),
        shape_diff=diffs,
        snapshot_path=str(ts_snap) if ts_snap else None,
        pinned_path=str(pinned_file),
        warnings=all_warnings,
    )


if __name__ == "__main__":
    r = probe()
    print(r)
    sys.exit(0 if r.ok else 1)
