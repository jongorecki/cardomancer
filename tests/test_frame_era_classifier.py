# tests/test_frame_era_classifier.py
# ---------------------------------------------------------------------------
# Validate frame_era_classifier.classify against the fixture set built by
# download_frame_fixtures.py. The fixture layout is
#
#   tests/fixtures/frame_classifier/<sig>/<name>_<set>_<col>_<id8>.png
#
# where <sig> is the ground-truth frame signature. Border color is
# recovered from the plan.csv row, which carries the Scryfall card_id.
# ---------------------------------------------------------------------------

import csv
import os
from collections import Counter, defaultdict

import cv2
import pytest

from frame_era_classifier import (
    FRAME_RETRO,
    FRAME_2003,
    FRAME_2015,
    FRAME_BORDERLESS,
    BORDER_BLACK,
    BORDER_BORDERLESS,
    BORDER_WHITE,
    classify,
)

FIXTURES = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures", "frame_classifier",
)
PLAN_CSV = os.path.join(FIXTURES, "plan.csv")

# Fixture signatures in plan.csv use Scryfall's 1993/1997 split; the
# classifier collapses those into "retro". Map truth labels before comparing.
_SIG_TO_ERA = {
    "1993": FRAME_RETRO,
    "1997": FRAME_RETRO,
    "2003": FRAME_2003,
    "2015": FRAME_2015,
    "borderless": FRAME_BORDERLESS,
}

EXPECTED_ACCURACY = {
    FRAME_RETRO: 0.90,
    FRAME_2003: 0.85,
    FRAME_2015: 0.85,
    FRAME_BORDERLESS: 0.65,
}
EXPECTED_BORDER_ACCURACY = 0.90  # White vs black is close to trivial.


def _fixture_rows():
    if not os.path.exists(PLAN_CSV):
        pytest.skip(f"{PLAN_CSV} not present — run download_frame_fixtures.py")
    rows = []
    with open(PLAN_CSV, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            path = os.path.join(FIXTURES, r["signature"], r["fixture_filename"])
            if os.path.exists(path):
                rows.append((path, r))
    if not rows:
        pytest.skip("Fixture PNGs not present — run download_frame_fixtures.py")
    return rows


def _border_truth(card_id: str, name: str, sig: str) -> str:
    """Read the ground-truth border_color from cards.CARD_DATA_BY_ID."""
    from cards import CARD_DATA_BY_ID
    c = CARD_DATA_BY_ID.get(card_id, {})
    bc = c.get("border_color", "")
    if sig == FRAME_BORDERLESS or bc == "borderless":
        return BORDER_BORDERLESS
    if bc == "white":
        return BORDER_WHITE
    return BORDER_BLACK


@pytest.fixture(scope="module")
def classifier_results():
    rows = _fixture_rows()
    out = []
    for path, r in rows:
        img = cv2.imread(path)
        if img is None:
            continue
        res = classify(img)
        out.append({
            "path": path,
            "name": r["name"],
            "card_id": r["card_id"],
            "truth_era": _SIG_TO_ERA.get(r["signature"], r["signature"]),
            "truth_border": _border_truth(r["card_id"], r["name"], r["signature"]),
            "pred_era": res["frame_era"],
            "pred_border": res["border_color"],
            "features": res["features"],
        })
    return out


def test_era_per_class_accuracy(classifier_results):
    by_truth = defaultdict(list)
    for r in classifier_results:
        by_truth[r["truth_era"]].append(r)

    summary = {}
    for era, items in by_truth.items():
        correct = sum(1 for r in items if r["pred_era"] == era)
        acc = correct / len(items)
        summary[era] = (correct, len(items), acc)

    print("\n=== Frame-era accuracy ===")
    for era in sorted(summary):
        c, n, a = summary[era]
        bar = EXPECTED_ACCURACY.get(era, 0.5)
        status = "OK" if a >= bar else "LOW"
        print(f"  {era:11s} {c:3d}/{n:3d} = {a:.1%}  (bar {bar:.0%}) [{status}]")

    failures = []
    for era, (c, n, a) in summary.items():
        bar = EXPECTED_ACCURACY.get(era, 0.5)
        if a < bar:
            failures.append(f"{era}: {a:.1%} < {bar:.0%}")
    assert not failures, "Per-class accuracy below bar: " + "; ".join(failures)


def test_era_confusion_matrix(classifier_results):
    """Print confusion matrix; no hard assertion — diagnostic only."""
    by_truth = defaultdict(Counter)
    for r in classifier_results:
        by_truth[r["truth_era"]][r["pred_era"]] += 1

    eras = [FRAME_RETRO, FRAME_2003, FRAME_2015, FRAME_BORDERLESS]
    print("\n=== Confusion (row=truth, col=pred) ===")
    header = "truth\\pred".ljust(12) + "".join(f"{e:>11s}" for e in eras)
    print(header)
    for t in eras:
        row_total = sum(by_truth[t].values())
        if row_total == 0:
            continue
        row = t.ljust(12) + "".join(
            f"{by_truth[t].get(p, 0):>11d}" for p in eras
        )
        print(row)


def test_border_accuracy(classifier_results):
    correct = sum(1 for r in classifier_results
                  if r["pred_border"] == r["truth_border"])
    total = len(classifier_results)
    acc = correct / total if total else 0.0
    print(f"\n=== Border-color accuracy: {correct}/{total} = {acc:.1%} ===")

    # Confusion
    cm = defaultdict(Counter)
    for r in classifier_results:
        cm[r["truth_border"]][r["pred_border"]] += 1
    borders = [BORDER_BLACK, BORDER_WHITE, BORDER_BORDERLESS]
    print("border confusion (row=truth, col=pred):")
    for t in borders:
        if sum(cm[t].values()) == 0:
            continue
        row = t.ljust(12) + "".join(f"{cm[t].get(p, 0):>11d}" for p in borders)
        print(row)

    assert acc >= EXPECTED_BORDER_ACCURACY, (
        f"Border accuracy {acc:.1%} below bar {EXPECTED_BORDER_ACCURACY:.0%}"
    )
