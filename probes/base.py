# probes/base.py
# ---------------------------------------------------------------------------
# Shared types + helpers for probe scripts. Every probe exposes a
# module-level probe() function returning a ProbeResult.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from config import SCRIPT_DIR

SNAPSHOT_DIR = Path(SCRIPT_DIR) / "tests" / "probe_snapshots"

DEFAULT_TIMEOUT_S = 30
USER_AGENT = "card-sorter-probe/0.1 (+https://example.invalid)"


@dataclass
class ProbeResult:
    """Uniform result shape for every probe script."""

    source: str
    ok: bool
    endpoint: str
    duration_ms: int
    shape_diff: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    snapshot_path: Optional[str] = None
    pinned_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "endpoint": self.endpoint,
            "duration_ms": self.duration_ms,
            "shape_diff": list(self.shape_diff),
            "warnings": list(self.warnings),
            "snapshot_path": self.snapshot_path,
            "pinned_path": self.pinned_path,
        }


def is_offline() -> bool:
    """Honor PROBES_OFFLINE=1 for CI / offline test runs."""
    return os.environ.get("PROBES_OFFLINE", "0") not in ("", "0", "false", "False")


def snapshot_path(source: str, *, pinned: bool = False) -> Path:
    """Standardised path for a probe's pinned or timestamped snapshot."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    if pinned:
        return SNAPSHOT_DIR / f"{source}_pinned.json"
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return SNAPSHOT_DIR / f"{source}_{ts}.json"


def write_snapshot(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True),
                    encoding="utf-8")


def load_pinned(source: str) -> Optional[Any]:
    """Return the pinned snapshot JSON for a source, or None if not pinned."""
    p = snapshot_path(source, pinned=True)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def compare_keys(a: Any, b: Any, path: str = "") -> list[str]:
    """Recursively compare the key SETS of two JSON-shaped objects.
    Returns a list of differing paths. Values are not compared — only
    structure, so expected-drift-free fixtures can match current data."""
    diffs: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        missing = set(a.keys()) - set(b.keys())
        extra = set(b.keys()) - set(a.keys())
        for k in sorted(missing):
            diffs.append(f"{path}.{k} (missing in latest)")
        for k in sorted(extra):
            diffs.append(f"{path}.{k} (new in latest)")
        for k in sorted(set(a.keys()) & set(b.keys())):
            diffs.extend(compare_keys(a[k], b[k], f"{path}.{k}"))
    elif isinstance(a, list) and isinstance(b, list):
        if a and b:
            diffs.extend(compare_keys(a[0], b[0], f"{path}[0]"))
    return diffs


def require_keys(obj: Any, required_paths: list[str]) -> list[str]:
    """Return a list of dotted-paths that are missing from obj.

    Paths use `.` for dict keys and `[]` for list-first-element lookup,
    e.g. "container.json_dict.card_lists[].cardviews[].name".
    """
    missing: list[str] = []
    for path in required_paths:
        if not _path_exists(obj, path):
            missing.append(path)
    return missing


def _path_exists(obj: Any, path: str) -> bool:
    cur = obj
    parts = _split_path(path)
    for p in parts:
        if p == "[]":
            if not isinstance(cur, list) or not cur:
                return False
            cur = cur[0]
        else:
            if not isinstance(cur, dict) or p not in cur:
                return False
            cur = cur[p]
    return True


def _split_path(path: str) -> list[str]:
    out: list[str] = []
    buf = ""
    i = 0
    while i < len(path):
        c = path[i]
        if c == ".":
            if buf:
                out.append(buf)
                buf = ""
        elif c == "[" and path[i:i + 2] == "[]":
            if buf:
                out.append(buf)
                buf = ""
            out.append("[]")
            i += 1  # skip the ']'
        else:
            buf += c
        i += 1
    if buf:
        out.append(buf)
    return out


def run_http_probe(
    source: str,
    endpoint: str,
    fetch: Callable[[], Any],
    required_paths: Optional[list[str]] = None,
) -> ProbeResult:
    """Shared harness: run fetch(), shape-check, pin-or-diff, return ProbeResult.

    Behavior:
      * PROBES_OFFLINE=1 — skip network, ok=True with a warning.
      * No pinned snapshot yet — write one on first successful fetch, ok=True.
      * Pinned snapshot exists — compare_keys vs current; diffs => ok=False.
      * Network / parse error — ok=False.
    """
    start = time.time()
    warnings: list[str] = []
    if is_offline():
        return ProbeResult(
            source=source,
            ok=True,
            endpoint=endpoint,
            duration_ms=int((time.time() - start) * 1000),
            warnings=["PROBES_OFFLINE=1 — network skipped."],
        )

    try:
        data = fetch()
    except Exception as e:
        return ProbeResult(
            source=source,
            ok=False,
            endpoint=endpoint,
            duration_ms=int((time.time() - start) * 1000),
            warnings=[f"Fetch failed: {type(e).__name__}: {e}"],
        )

    # Required-path contract
    missing = require_keys(data, required_paths or [])
    if missing:
        ts_path = snapshot_path(source)
        try:
            write_snapshot(ts_path, data)
        except Exception:
            ts_path = None  # type: ignore[assignment]
        return ProbeResult(
            source=source,
            ok=False,
            endpoint=endpoint,
            duration_ms=int((time.time() - start) * 1000),
            shape_diff=[f"missing required path: {p}" for p in missing],
            snapshot_path=str(ts_path) if ts_path else None,
            warnings=warnings,
        )

    pinned = load_pinned(source)
    pinned_file = snapshot_path(source, pinned=True)

    if pinned is None:
        # First successful run — pin the snapshot.
        write_snapshot(pinned_file, data)
        warnings.append("Pinned initial snapshot on first successful probe.")
        return ProbeResult(
            source=source,
            ok=True,
            endpoint=endpoint,
            duration_ms=int((time.time() - start) * 1000),
            pinned_path=str(pinned_file),
            warnings=warnings,
        )

    diffs = compare_keys(pinned, data)
    ts_path = snapshot_path(source)
    try:
        write_snapshot(ts_path, data)
    except Exception:
        ts_path = None  # type: ignore[assignment]

    return ProbeResult(
        source=source,
        ok=not diffs,
        endpoint=endpoint,
        duration_ms=int((time.time() - start) * 1000),
        shape_diff=diffs,
        snapshot_path=str(ts_path) if ts_path else None,
        pinned_path=str(pinned_file),
        warnings=warnings,
    )
