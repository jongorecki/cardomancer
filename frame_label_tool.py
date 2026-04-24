# frame_label_tool.py
# ---------------------------------------------------------------------------
# Minimal Flask app for manually labeling the frame era of card scans,
# so we have a real source of truth for validating the frame classifier.
#
# Priority queue:
#   1. Scans with multi-era same-art candidates (the only ones where the
#      cascade's frame stage actually fires).
#   2. Everything else, ordered by least-labeled (era, color) group.
#
# Labels are appended to tests/fixtures/frame_classifier/manual_labels.csv
# (resumable — existing rows are loaded on startup, and already-labeled
# paths are skipped).
#
# Run:    python frame_label_tool.py
# Then open:    http://localhost:5055/
# ---------------------------------------------------------------------------

import csv
import glob
import os
import sys
from collections import Counter, defaultdict

from flask import Flask, jsonify, render_template, request, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cards import (
    CARD_DATA_BY_ID,
    CARDS_DATA,
    get_illustration_id,
)
from config import EXCLUDED_SETS
from frame_template_matcher import card_color_category, card_frame_era

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LABELS_CSV = os.path.join(
    SCRIPT_DIR, "tests", "fixtures", "frame_classifier", "manual_labels.csv"
)
SCAN_LOGS = os.path.join(SCRIPT_DIR, "scan_logs")

VALID_LABELS = {"retro", "2003", "2015", "borderless", "unsure", "skip"}

app = Flask(__name__, template_folder="templates")

# ---------------------------------------------------------------------------
# State loaded at startup
# ---------------------------------------------------------------------------

# queue: list of dicts describing each scan to label.
#   { path, scan_num, session, set, collector_number, card_name,
#     scryfall_era, scryfall_color, candidate_eras, ambiguous }
# `queue_order` is index list indicating show order (priority first).
# `labeled_paths` is the set of paths already in LABELS_CSV.
queue: list = []
queue_order: list = []
labeled_paths: set = set()
existing_labels: dict = {}  # path -> label (for review/edit)


def _build_card_index():
    idx = {}
    for cid, c in CARD_DATA_BY_ID.items():
        idx[(c.get("set", "").lower(), str(c.get("collector_number", "")))] = c
    return idx


def _build_illus_index():
    """illustration_id -> list of card_ids (English, paper, non-excluded).
    Built once so _scan_rows doesn't do 1409 O(N) scans."""
    idx = defaultdict(list)
    for c in CARDS_DATA:
        illus = c.get("illustration_id")
        if not illus:
            continue
        if c.get("lang") != "en":
            continue
        set_code = c.get("set", "").lower()
        if set_code in EXCLUDED_SETS:
            continue
        if "paper" not in (c.get("games") or []):
            continue
        idx[illus].append(c.get("id"))
    return idx


def _load_existing_labels():
    labels = {}
    if not os.path.exists(LABELS_CSV):
        return labels
    with open(LABELS_CSV, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            labels[r["path"]] = r["label"]
    return labels


def _scan_rows(card_idx: dict, illus_idx: dict) -> list:
    """Walk scan_logs/session_*/scans.csv and produce one queue entry per
    recognized scan. Attaches Scryfall metadata (era, color, same-art
    candidate eras) so the labeling UI can show context."""
    rows = []
    for session in sorted(glob.glob(os.path.join(SCAN_LOGS, "session_*"))):
        csv_path = os.path.join(session, "scans.csv")
        if not os.path.exists(csv_path):
            continue
        with open(csv_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("recognized", "").lower() != "true":
                    continue
                setc = r.get("set", "").lower()
                num = r.get("collector_number", "")
                card = card_idx.get((setc, num))
                if not card:
                    continue
                crop = os.path.join(
                    session, "card_crops",
                    f"card_{int(r['scan_num']):04d}.jpg",
                )
                if not os.path.exists(crop):
                    continue
                era = card_frame_era(card)
                color = card_color_category(card)
                illus = get_illustration_id(card.get("id"))
                cand_eras = set()
                if illus:
                    for cid in illus_idx.get(illus, []):
                        cc = CARD_DATA_BY_ID.get(cid)
                        if cc:
                            ce = card_frame_era(cc)
                            if ce:
                                cand_eras.add(ce)
                rows.append({
                    "path": crop,
                    "scan_num": int(r["scan_num"]),
                    "session": os.path.basename(session),
                    "set": r.get("set", ""),
                    "collector_number": r.get("collector_number", ""),
                    "card_name": r.get("name", ""),
                    "scryfall_era": era,
                    "scryfall_color": color,
                    "candidate_eras": sorted(cand_eras),
                    "ambiguous": len(cand_eras) >= 2,
                })
    return rows


def _priority_order(rows: list, already_labeled: set) -> list:
    """
    Return index list into `rows` sorted so that:
      1. Multi-era ambiguous scans come first.
      2. Within each group, rarer (era, color) combinations first, so we
         fill sparse classes quickest.
      3. Already-labeled rows go to the end.
    """
    group_counts = Counter()
    for r in rows:
        group_counts[(r["scryfall_era"], r["scryfall_color"])] += 1

    def sort_key(i):
        r = rows[i]
        done = r["path"] in already_labeled
        ambig = 0 if r["ambiguous"] else 1
        rarity = group_counts[(r["scryfall_era"], r["scryfall_color"])]
        return (done, ambig, rarity, r["session"], r["scan_num"])

    return sorted(range(len(rows)), key=sort_key)


def _reload_state():
    global queue, queue_order, labeled_paths, existing_labels
    existing_labels = _load_existing_labels()
    labeled_paths = set(existing_labels.keys())
    card_idx = _build_card_index()
    illus_idx = _build_illus_index()
    queue = _scan_rows(card_idx, illus_idx)
    queue_order = _priority_order(queue, labeled_paths)


def _append_label(row: dict, label: str, note: str = "") -> None:
    os.makedirs(os.path.dirname(LABELS_CSV), exist_ok=True)
    new_file = not os.path.exists(LABELS_CSV)
    with open(LABELS_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow([
                "path", "label", "scryfall_era", "scryfall_color",
                "card_name", "set", "collector_number",
                "candidate_eras", "note",
            ])
        w.writerow([
            row["path"], label, row["scryfall_era"], row["scryfall_color"],
            row["card_name"], row["set"], row["collector_number"],
            ",".join(row["candidate_eras"]), note,
        ])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("frame_label.html")


@app.route("/api/next")
def api_next():
    """Return the next unlabeled scan, or a "done" payload."""
    for idx in queue_order:
        r = queue[idx]
        if r["path"] in labeled_paths:
            continue
        return jsonify({
            "done": False,
            "row": r,
            "remaining": sum(
                1 for i in queue_order if queue[i]["path"] not in labeled_paths
            ),
            "labeled": len(labeled_paths),
            "total": len(queue),
        })
    return jsonify({"done": True, "labeled": len(labeled_paths),
                    "total": len(queue)})


@app.route("/api/image")
def api_image():
    path = request.args.get("path", "")
    # Defensive: only serve paths we know about.
    if path not in {r["path"] for r in queue}:
        return "not found", 404
    return send_file(path, mimetype="image/jpeg")


@app.route("/api/label", methods=["POST"])
def api_label():
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    label = data.get("label")
    note = data.get("note", "")
    if label not in VALID_LABELS:
        return jsonify({"error": f"invalid label {label!r}"}), 400
    rows_by_path = {r["path"]: r for r in queue}
    r = rows_by_path.get(path)
    if r is None:
        return jsonify({"error": "unknown path"}), 404
    _append_label(r, label, note=note)
    labeled_paths.add(path)
    existing_labels[path] = label
    return jsonify({"ok": True, "labeled": len(labeled_paths)})


@app.route("/api/stats")
def api_stats():
    by_label = Counter(existing_labels.values())
    # Progress by (era, color) for ambiguous set
    amb_labeled = Counter()
    amb_total = Counter()
    for r in queue:
        if not r["ambiguous"]:
            continue
        key = (r["scryfall_era"], r["scryfall_color"])
        amb_total[key] += 1
        if r["path"] in labeled_paths:
            amb_labeled[key] += 1
    breakdown = []
    for key in sorted(amb_total.keys()):
        breakdown.append({
            "era": key[0], "color": key[1],
            "labeled": amb_labeled[key], "total": amb_total[key],
        })
    return jsonify({
        "total_scans": len(queue),
        "total_labeled": len(labeled_paths),
        "total_ambiguous": sum(1 for r in queue if r["ambiguous"]),
        "ambiguous_labeled": sum(
            1 for r in queue if r["ambiguous"] and r["path"] in labeled_paths
        ),
        "by_label": dict(by_label),
        "ambiguous_breakdown": breakdown,
    })


def main():
    _reload_state()
    stats = {
        "total": len(queue),
        "labeled": len(labeled_paths),
        "ambiguous": sum(1 for r in queue if r["ambiguous"]),
    }
    print(f"[frame_label_tool] {stats['total']} total scans, "
          f"{stats['ambiguous']} ambiguous, "
          f"{stats['labeled']} already labeled.")
    print(f"[frame_label_tool] Labels CSV: {LABELS_CSV}")
    print("[frame_label_tool] Open http://localhost:5055/")
    app.run(host="127.0.0.1", port=5055, debug=False)


if __name__ == "__main__":
    main()
