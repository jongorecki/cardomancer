# diag_printing.py
# ---------------------------------------------------------------------------
# Phase 7 validation + threshold-tuning harness for the printing
# disambiguation cascade.
#
# Walks a set of scan_logs sessions, joins each scan_num to its
# card_crops/card_NNNN.jpg rectified crop and its scans.csv row, resolves
# (set, collector_number) -> Scryfall card_id, and runs
# `printing_disambiguation.disambiguate_printing` against the crop.
# Emits a CSV with per-stage confidences + correctness so thresholds can
# be tuned offline.
#
# Usage
# -----
#   python diag_printing.py                       # scans 2026-04-20+ sessions
#   python diag_printing.py --since 2026-04-15
#   python diag_printing.py --session session_20260421_140152
#   python diag_printing.py --out diag_output/printing_20260423.csv
#
# Output columns
# --------------
#   session, scan_num, name, ground_truth_set, all_sets,
#   resolved_card_id, predicted_card_id, predicted_set, correct,
#   source, frame_confidence, stamp_confidence, stamp_has_stamp,
#   icon_confidence, icon_set_pick, n_candidates,
#   cascade_relevant
#
# Only rows where `all_sets` has >= 2 sets (cascade_relevant=True) are
# the ones that actually exercise the cascade; singles are included for
# completeness but should all trivially return source=single_match.
# ---------------------------------------------------------------------------

import argparse
import csv
import glob
import os
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cards import CARDS_DATA, CARD_DATA_BY_ID  # noqa: E402
from printing_disambiguation import disambiguate_printing  # noqa: E402

SCAN_LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_logs")


def _build_lookup() -> Dict[Tuple[str, str], str]:
    """(set_code, collector_number) -> card_id for English paper cards."""
    idx: Dict[Tuple[str, str], str] = {}
    for c in CARDS_DATA:
        if c.get("lang") != "en":
            continue
        s = c.get("set")
        n = c.get("collector_number")
        cid = c.get("id")
        if s and n and cid:
            idx[(s, str(n))] = cid
    return idx


def _session_date(session_name: str) -> str:
    """'session_20260421_140152' -> '20260421'."""
    parts = session_name.split("_")
    return parts[1] if len(parts) >= 2 else ""


def _find_sessions(since: Optional[str],
                   only: Optional[str]) -> List[str]:
    sessions = sorted(glob.glob(os.path.join(SCAN_LOGS_DIR, "session_*")))
    if only:
        sessions = [s for s in sessions if os.path.basename(s) == only]
    if since:
        sessions = [s for s in sessions
                    if _session_date(os.path.basename(s)) >= since]
    return [s for s in sessions
            if os.path.isfile(os.path.join(s, "scans.csv"))
            and os.path.isdir(os.path.join(s, "card_crops"))]


def _iter_scans(session_dir: str):
    """Yield (scan_num, row_dict, crop_path) for every row that has a crop."""
    csv_path = os.path.join(session_dir, "scans.csv")
    crops_dir = os.path.join(session_dir, "card_crops")
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                num = int(row["scan_num"])
            except (KeyError, ValueError, TypeError):
                continue
            crop = os.path.join(crops_dir, f"card_{num:04d}.jpg")
            if os.path.exists(crop):
                yield num, row, crop


def run_diag(sessions: List[str],
             out_path: str,
             lookup: Dict[Tuple[str, str], str]) -> List[dict]:
    rows: List[dict] = []
    for sdir in sessions:
        name = os.path.basename(sdir)
        print(f"[diag] {name} ...")
        for num, row, crop_path in _iter_scans(sdir):
            set_code = (row.get("set") or "").strip()
            collector = (row.get("collector_number") or "").strip()
            all_sets = (row.get("all_sets") or "").strip()
            sets_list = [s for s in all_sets.split(";") if s]
            cascade_relevant = len(sets_list) >= 2
            card_id = lookup.get((set_code, collector))

            rec = {
                "session": name,
                "scan_num": num,
                "name": (row.get("name") or "").strip(),
                "ground_truth_set": set_code,
                "all_sets": all_sets,
                "n_candidates": len(sets_list),
                "cascade_relevant": cascade_relevant,
                "resolved_card_id": card_id or "",
                "predicted_card_id": "",
                "predicted_set": "",
                "correct": "",
                "source": "",
                "frame_confidence": "",
                "stamp_confidence": "",
                "stamp_has_stamp": "",
                "icon_confidence": "",
                "icon_set_pick": "",
            }

            if not card_id:
                rec["source"] = "unresolved_card_id"
                rows.append(rec)
                continue

            img = cv2.imread(crop_path, cv2.IMREAD_COLOR)
            if img is None:
                rec["source"] = "crop_unreadable"
                rows.append(rec)
                continue

            try:
                result = disambiguate_printing(img, card_id)
            except Exception as e:
                rec["source"] = f"exception:{type(e).__name__}"
                rows.append(rec)
                continue

            pred_id = result.get("final_card_id") or ""
            pred_card = CARD_DATA_BY_ID.get(pred_id) if pred_id else None
            pred_set = (pred_card or {}).get("set", "")
            rec.update({
                "predicted_card_id": pred_id,
                "predicted_set": pred_set,
                "correct": str(pred_set == set_code),
                "source": result.get("source", ""),
                "frame_confidence": _fmt(result.get("frame_confidence")),
                "stamp_confidence": _fmt(result.get("stamp_confidence")),
                "stamp_has_stamp": _fmt(result.get("stamp_has_stamp")),
                "icon_confidence": _fmt(result.get("icon_confidence")),
                "icon_set_pick": result.get("icon_set_pick") or "",
            })
            rows.append(rec)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"[diag] Wrote {len(rows)} rows -> {out_path}")
    return rows


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def summarize(rows: List[dict]) -> None:
    cascade_rows = [r for r in rows if r["cascade_relevant"]]
    resolved = [r for r in cascade_rows if r["resolved_card_id"]]
    evaluable = [r for r in resolved if r["predicted_card_id"]]
    correct = [r for r in evaluable if r["correct"] == "True"]

    print()
    print("=" * 60)
    print(f"Total rows           : {len(rows)}")
    print(f"Cascade-relevant     : {len(cascade_rows)}")
    print(f"Resolved to card_id  : {len(resolved)}")
    print(f"Cascade ran OK       : {len(evaluable)}")
    if evaluable:
        print(f"Correct predictions  : {len(correct)} "
              f"({100 * len(correct) / len(evaluable):.1f}%)")
    print()
    by_source = Counter(r["source"] for r in evaluable)
    print("Predictions by cascade source:")
    for src, n in by_source.most_common():
        n_correct = sum(
            1 for r in evaluable
            if r["source"] == src and r["correct"] == "True"
        )
        pct = f"{100 * n_correct / n:.0f}%" if n else "-"
        print(f"  {src:<28} {n:>4}   correct={n_correct} ({pct})")
    print()
    wrong = [r for r in evaluable if r["correct"] == "False"]
    committed_wrong = [
        r for r in wrong
        if r["source"] in (
            "frame_disambiguated", "stamp_disambiguated",
            "icon_disambiguated", "fully_disambiguated"
        )
    ]
    print(f"Wrong total          : {len(wrong)}")
    print(f"Wrong + high-conf    : {len(committed_wrong)}  "
          f"(cascade committed to wrong pick — tighten thresholds)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="20260420",
                        help="Only sessions on/after this date (YYYYMMDD). "
                             "Default 20260420 — the new-lighting cutoff.")
    parser.add_argument("--session", default=None,
                        help="Restrict to one session directory name.")
    parser.add_argument("--out", default="diag_output/printing_diag.csv",
                        help="Output CSV path.")
    args = parser.parse_args(argv)

    sessions = _find_sessions(args.since, args.session)
    if not sessions:
        print(f"[diag] No sessions matched since={args.since} only={args.session}")
        return 1
    print(f"[diag] {len(sessions)} session(s)")

    lookup = _build_lookup()
    print(f"[diag] Built {len(lookup)} (set, collector) -> card_id entries")

    rows = run_diag(sessions, args.out, lookup)
    summarize(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
