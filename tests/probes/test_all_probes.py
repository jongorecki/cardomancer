# tests/probes/test_all_probes.py
# ---------------------------------------------------------------------------
# Asserts every data source in web_enrichment_source_probes.md has a
# matching probes/probe_<source>.py file, and that probes/run_all.py
# exits cleanly under the Phase 0A stubs.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
PROBES_DIR = _ROOT / "probes"

EXPECTED_SOURCES = {
    "scryfall_bulk",
    "scryfall_search",
    "tagger",
    "edhrec",
    "edhtop16",
    "spellbook",
    "moxfield",
    "buylist_ck",
}


def test_probe_script_exists_for_every_source():
    found = {
        p.stem.replace("probe_", "")
        for p in PROBES_DIR.glob("probe_*.py")
    }
    missing = EXPECTED_SOURCES - found
    assert not missing, f"Missing probe scripts: {missing}"


def test_run_all_exits_zero():
    """Offline probe pass — PROBES_OFFLINE skips network but must still exit 0."""
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(_ROOT))
    env["PROBES_OFFLINE"] = "1"
    result = subprocess.run(
        [sys.executable, str(PROBES_DIR / "run_all.py")],
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"run_all.py failed (stdout={result.stdout!r} stderr={result.stderr!r})"
    )


@pytest.mark.live
def test_run_all_against_live_network():
    """Full live run. Skipped unless `-m live` is passed."""
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(_ROOT))
    result = subprocess.run(
        [sys.executable, str(PROBES_DIR / "run_all.py")],
        capture_output=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout.decode(errors="replace")
