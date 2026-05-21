#!/usr/bin/env python3
"""
Simulate a long sort session and report ScanTracker memory growth.

This is a one-shot diagnostic — not a CI fixture. Run it after touching
anything in the scan-tracking or per-scan persistence paths to confirm
we haven't reintroduced an unbounded per-scan in-memory buffer.

Usage:
    python -m tools.memory_profile_session                    # 10k scans
    python -m tools.memory_profile_session --scans 50000      # stress
    python -m tools.memory_profile_session --top 20           # noisier

Prints:
  - Total Python heap delta from session start to scan_count cards.
  - The top N tracemalloc allocation sources by retained size.
  - Per-bin and per-attribute breakdown of the ScanTracker state, so
    we can spot the moment a buffer grows non-linearly.

What we know we DON'T leak (kept bounded by design):
  - sort_times in CardSorterWorker is capped at 50.
  - bin_card_counts / overflow_map are bounded by physical-bin count.
  - tracker.bins is bounded by `cards landed` which equals scan_count
    but each entry is a tiny dict (name/set/cnum), and most operators
    will empty their bins or stop the session before it matters.

What used to leak (fixed):
  - ScanTracker.scans appended a 20-field dict per scan with no
    upper bound. Removed in favour of running counters + CSV/DB
    persistence.
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import tempfile
import tracemalloc
import unittest.mock as mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _install_hw_mocks():
    """Mock hardware deps so this script imports cleanly."""
    if not isinstance(sys.modules.get('gcode_control'), mock.MagicMock):
        gcode_mock = mock.MagicMock()
        gcode_mock.is_connected = lambda: False
        gcode_mock.get_bin_locations = lambda: {}
        sys.modules['gcode_control'] = gcode_mock


def _build_fake_card(scan_num: int) -> tuple[dict, dict]:
    """Synthesize the per-scan card_info + card_data dicts. Fields
    match what record_scan() actually reads."""
    bin_num = (scan_num % 10) + 1
    card_info = {
        'Name':    f'Sim Card {scan_num}',
        'Set':     f's{scan_num % 200:03d}',
        'Sets':    [f's{scan_num % 200:03d}'],
        'Colors':  ['R'],
        'CMC':     float(scan_num % 8),
        'Types':   'Creature',
        'Rarity':  ['common', 'uncommon', 'rare'][scan_num % 3],
        'Price':   f'{(scan_num % 50) * 0.10:.2f}',
        'Id':      f'sim-{scan_num}',
    }
    card_data = {
        'collector_number': str(scan_num % 300),
        'type_line':        'Creature — Bird',
        'oracle_id':        f'oid-{scan_num}',
    }
    return card_info, card_data, bin_num


def run(scan_count: int, top: int) -> None:
    _install_hw_mocks()
    import scan_tracker
    import collection_db

    # Throwaway scan log + collection DB so the file/DB I/O part of
    # record_scan runs end-to-end (matching production paths).
    tmpdir = tempfile.mkdtemp(prefix='cm_mem_profile_')
    sub_logs = os.path.join(tmpdir, 'scan_logs')
    sub_db = os.path.join(tmpdir, 'collection.db')
    os.makedirs(sub_logs, exist_ok=True)

    # Point scan_tracker at the throwaway logs dir + DB.
    from config import SCAN_LOGS_DIR as _orig_logs
    scan_tracker.SCAN_LOGS_DIR = sub_logs

    tracker = scan_tracker.ScanTracker(db_path=sub_db)
    tracker.start_session(sort_mode='memprofile')

    tracemalloc.start()
    snap_start = tracemalloc.take_snapshot()

    for i in range(1, scan_count + 1):
        ci, cd, bn = _build_fake_card(i)
        tracker.record_scan(
            card_info=ci,
            bin_num=bn,
            method='hash',
            hash_distance=20.0 + (i % 50),
            card_data=cd,
            is_foil=(i % 7 == 0),
            foil_confidence=0.5 + (i % 5) / 10.0,
            frame='2015',
            border_color='black',
            frame_effects=[],
        )
        if i % 1000 == 0:
            print(f"  ... {i:>6} scans recorded", flush=True)

    snap_end = tracemalloc.take_snapshot()
    tracemalloc.stop()

    # ScanTracker shape report
    print(f"\n=== ScanTracker post-{scan_count}-scan shape ===")
    print(f"  scan_count:             {tracker.scan_count}")
    print(f"  unrecognized_count:     {tracker.unrecognized_count}")
    print(f"  session_total_value:    {tracker.session_total_value:.2f}")
    print(f"  bins (count):           {len(tracker.bins)}")
    bins_total_entries = sum(len(v) for v in tracker.bins.values())
    print(f"  bins (total entries):   {bins_total_entries}")
    print(f"  has self.scans attr:    {hasattr(tracker, 'scans')}")
    # If the fix is intact, tracker has no `scans` attr at all.

    # Memory delta
    stats = snap_end.compare_to(snap_start, 'lineno')
    print(f"\n=== Top {top} growth lines ===")
    total_growth = 0
    for stat in stats[:top]:
        total_growth += stat.size_diff
        print(f"  +{stat.size_diff / 1024:8.1f} KiB  "
              f"{stat.traceback.format()[-1]}")
    print(f"\n  Total +growth across all sources: "
          f"{total_growth / 1024 / 1024:.2f} MiB")

    # Cleanup
    tracker.end_session()
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scans', type=int, default=10_000,
                   help='Number of scans to simulate (default: 10000)')
    p.add_argument('--top', type=int, default=10,
                   help='Top N tracemalloc growth lines to print')
    args = p.parse_args(argv)
    print(f"Simulating {args.scans} scans...")
    run(args.scans, args.top)
    return 0


if __name__ == '__main__':
    sys.exit(main())
