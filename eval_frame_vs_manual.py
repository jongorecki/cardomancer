# eval_frame_vs_manual.py
# ---------------------------------------------------------------------------
# Measure template-matcher + detect_frame accuracy against the manually
# labeled ground truth in tests/fixtures/frame_classifier/manual_labels.csv.
# ---------------------------------------------------------------------------

import csv
import os
from collections import Counter, defaultdict

import cv2

from cards import CARD_DATA_BY_ID
from frame_detect import detect_frame
from frame_template_matcher import FrameTemplateMatcher


def era_from_frame(frame_str, frame_effects):
    fe = frame_effects or ()
    if "borderless" in fe:
        return "borderless"
    if frame_str in ("1993", "1997"):
        return "retro"
    if frame_str == "2003":
        return "2003"
    if frame_str == "2015":
        return "2015"
    return None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LABELS_CSV = os.path.join(
    SCRIPT_DIR, "tests", "fixtures", "frame_classifier", "manual_labels.csv"
)
SCAN_LOGS = os.path.join(SCRIPT_DIR, "scan_logs")

SKIP_LABELS = {"skip", "unsure"}


def build_card_idx():
    idx = {}
    for cid, c in CARD_DATA_BY_ID.items():
        idx[(c.get("set", "").lower(), str(c.get("collector_number", "")))] = c
    return idx


def main():
    card_idx = build_card_idx()
    print("[build] building template matcher from scan_logs...")
    matcher, _ = FrameTemplateMatcher.from_scan_dir(SCAN_LOGS, card_idx)
    print(f"[build] {len(matcher.templates)} templates")

    with open(LABELS_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    rows = [r for r in rows if r["label"] not in SKIP_LABELS]
    print(f"[eval] {len(rows)} labeled rows (after dropping skip/unsure)")

    overall = Counter()
    df_overall = Counter()
    by_candset = defaultdict(Counter)
    df_by_candset = defaultdict(Counter)
    by_scry_era = defaultdict(Counter)
    by_color = defaultdict(Counter)
    confusion = Counter()  # (true, pred) -> count
    df_confusion = Counter()

    for r in rows:
        img = cv2.imread(r["path"])
        if img is None:
            continue
        truth = r["label"]
        color = r["scryfall_color"] or None
        cand = tuple(sorted(r["candidate_eras"].split(","))) if r["candidate_eras"] else ("",)

        # Template matcher
        res = matcher.classify(img, color=color)
        pred = res["frame_era"]
        ok = (pred == truth)
        overall["correct" if ok else "wrong"] += 1
        by_candset[cand]["correct" if ok else "wrong"] += 1
        by_scry_era[r["scryfall_era"]]["correct" if ok else "wrong"] += 1
        by_color[color or "?"]["correct" if ok else "wrong"] += 1
        confusion[(truth, pred)] += 1

        # detect_frame baseline (unrestricted)
        df_res = detect_frame(img)
        df_pred = era_from_frame(df_res.get("best_frame"), df_res.get("best_frame_effects"))
        df_ok = (df_pred == truth)
        df_overall["correct" if df_ok else "wrong"] += 1
        df_by_candset[cand]["correct" if df_ok else "wrong"] += 1
        df_confusion[(truth, df_pred or "none")] += 1

    def pct(c):
        t = c["correct"] + c["wrong"]
        return f"{c['correct']}/{t} = {100*c['correct']/max(1,t):.1f}%"

    print("\n=== Template matcher vs manual labels ===")
    print("  overall:", pct(overall))
    print("\n=== detect_frame (unrestricted) vs manual labels ===")
    print("  overall:", pct(df_overall))

    print("\nMulti-era subset (the cases where frame actually disambiguates):")
    tm_multi = Counter()
    df_multi = Counter()
    for k, v in by_candset.items():
        if len(k) >= 2:
            tm_multi.update(v)
    for k, v in df_by_candset.items():
        if len(k) >= 2:
            df_multi.update(v)
    print(f"  template matcher: {pct(tm_multi)}")
    print(f"  detect_frame    : {pct(df_multi)}")

    print("\nPer candidate-era-set (template matcher | detect_frame):")
    for k in sorted(by_candset, key=lambda x: -(by_candset[x]["correct"] + by_candset[x]["wrong"])):
        if len(k) < 2:
            continue
        print(f"  {str(k):40s} tm: {pct(by_candset[k]):20s} df: {pct(df_by_candset[k])}")

    print("\nBy scryfall_era (what the cascade thought it was):")
    for k in sorted(by_scry_era):
        print(f"  {k:12s} {pct(by_scry_era[k])}")

    print("\nBy candidate-era-set (only multi-era = cascade actually needs frame):")
    multi = {k: v for k, v in by_candset.items() if len(k) >= 2}
    for k in sorted(multi, key=lambda x: -(multi[x]["correct"] + multi[x]["wrong"])):
        print(f"  {str(k):40s} {pct(multi[k])}")

    print("\nBy scryfall color:")
    for k in sorted(by_color):
        print(f"  {k:3s} {pct(by_color[k])}")

    print("\nConfusion (true -> pred):")
    truths = sorted({t for t, _ in confusion})
    preds = sorted({p for _, p in confusion})
    hdr = "true \\ pred".ljust(14) + "".join(f"{p:>12s}" for p in preds)
    print(hdr)
    for t in truths:
        row = t.ljust(14) + "".join(f"{confusion[(t,p)]:>12d}" for p in preds)
        print(row)


if __name__ == "__main__":
    main()
