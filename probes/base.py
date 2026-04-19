# probes/base.py
# ---------------------------------------------------------------------------
# Shared types + helpers for probe scripts. Every probe exposes a
# module-level probe() function returning a ProbeResult.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import SCRIPT_DIR

SNAPSHOT_DIR = Path(SCRIPT_DIR) / "tests" / "probe_snapshots"


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
