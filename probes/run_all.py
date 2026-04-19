# probes/run_all.py
# ---------------------------------------------------------------------------
# Run every probe script and print a summary. Exit code: 0 if every
# probe returned ok=True (including stubs), non-zero otherwise. Intended
# for use as a pre-refresh hook and in CI / pytest.
# ---------------------------------------------------------------------------

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path when invoked as a script.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from probes.base import ProbeResult  # noqa: E402


PROBE_MODULES = [
    "probes.probe_scryfall_bulk",
    "probes.probe_scryfall_search",
    "probes.probe_tagger",
    "probes.probe_edhrec",
    "probes.probe_edhtop16",
    "probes.probe_spellbook",
    "probes.probe_moxfield",
    "probes.probe_buylist_ck",
]


def run_all() -> tuple[int, list[ProbeResult]]:
    results: list[ProbeResult] = []
    for mod_name in PROBE_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            results.append(ProbeResult(
                source=mod_name.split(".")[-1].replace("probe_", ""),
                ok=False,
                endpoint="<import failed>",
                duration_ms=0,
                shape_diff=[f"ImportError: {type(e).__name__}: {e}"],
            ))
            continue
        try:
            r = mod.probe()
        except Exception as e:
            r = ProbeResult(
                source=getattr(mod, "SOURCE", mod_name),
                ok=False,
                endpoint=getattr(mod, "ENDPOINT", "<unknown>"),
                duration_ms=0,
                shape_diff=[f"{type(e).__name__}: {e}"],
            )
        results.append(r)

    exit_code = 0 if all(r.ok for r in results) else 1
    return exit_code, results


def _print_summary(results: list[ProbeResult]) -> None:
    print("Probe summary")
    print("-" * 72)
    for r in results:
        status = "OK  " if r.ok else "FAIL"
        print(f"  [{status}] {r.source:<18} {r.endpoint}")
        for w in r.warnings:
            print(f"           · {w}")
        for d in r.shape_diff:
            print(f"           ! {d}")
    print("-" * 72)


def main() -> int:
    exit_code, results = run_all()
    if os.environ.get("PROBES_JSON") == "1":
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        _print_summary(results)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
