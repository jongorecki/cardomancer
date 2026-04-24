# diag_set_symbol_fails.py
# ---------------------------------------------------------------------------
# Diagnostic script to analyze why set-symbol matcher fails on woe/blb/bro.
#
# For each target set, finds up to 5 failing scans and generates diagnostic
# PNG tiles showing: raw ROI, Canny edges, correct template, predicted
# template, with per-tile metrics.
# ---------------------------------------------------------------------------
import csv
import glob
import os
from collections import Counter, defaultdict
from typing import List, Tuple

import cv2
import numpy as np

from cards import CARD_DATA_BY_ID, CARDS_DATA
from config import EXCLUDED_SETS
from set_symbol_roi import get_symbol_roi
from eval_set_symbol_matcher import (
    load_templates,
    preprocess_roi,
    match_score,
    build_card_idx,
    build_illus_reprint_counts,
    ROI_PAD,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCAN_LOGS = os.path.join(SCRIPT_DIR, "scan_logs")
OUT_DIR = os.path.join(SCRIPT_DIR, "tmp", "diag_set_symbol_fails")

TARGET_SETS = {"woe", "blb", "bro"}
MAX_FAILS_PER_SET = 5
UPSCALE_FACTOR = 3


def collect_failures():
    """Return {set_code: [(scan_id, image_path, true_set, roi, scores)]}.

    scores = [(set_code, score), ...]
    """
    templates = load_templates(use_edge=True)
    card_idx = build_card_idx()
    illus_counts = build_illus_reprint_counts()
    template_sets = {code for (code, _) in templates.keys()}

    failures = defaultdict(list)

    for session in sorted(glob.glob(os.path.join(SCAN_LOGS, "session_*"))):
        csv_path = os.path.join(session, "scans.csv")
        if not os.path.exists(csv_path):
            continue

        with open(csv_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("recognized", "").lower() != "true":
                    continue

                set_code = r.get("set", "").lower()
                if set_code not in TARGET_SETS:
                    continue
                if set_code not in template_sets:
                    continue

                card = card_idx.get((set_code, r.get("collector_number", "")))
                if not card:
                    continue
                if card.get("frame_effects") or "Basic Land" in (card.get("type_line") or ""):
                    continue

                iid = card.get("illustration_id")
                if not iid or illus_counts.get(iid, 0) != 1:
                    continue

                frame = card.get("frame", "")
                roi = get_symbol_roi(frame, None)
                if roi is None:
                    continue

                crop_path = os.path.join(
                    session, "card_crops",
                    f"card_{int(r['scan_num']):04d}.jpg",
                )
                if not os.path.exists(crop_path):
                    continue

                img = cv2.imread(crop_path)
                if img is None:
                    continue

                edge_roi = preprocess_roi(img, roi, use_edge=True)
                if edge_roi.size == 0:
                    continue

                # Score same-frame candidates
                cand_keys = [(s, f) for (s, f) in templates.keys() if f == frame]
                scores = []
                for key in cand_keys:
                    tmpl = templates[key]
                    s = match_score(edge_roi, tmpl)
                    if s < 0:
                        continue
                    scores.append((key[0], s))  # just (set_code, score)

                if not scores:
                    continue

                scores.sort(key=lambda x: -x[1])
                pred_set = scores[0][0]

                if pred_set != set_code:  # This is a failure
                    failures[set_code].append({
                        "scan_num": r["scan_num"],
                        "crop_path": crop_path,
                        "true_set": set_code,
                        "roi": roi,
                        "frame": frame,
                        "scores": scores,
                    })

    return failures


def assemble_diagnostic_tile(
    scan_id: str,
    raw_crop: np.ndarray,
    edge_crop: np.ndarray,
    correct_set: str,
    predicted_set: str,
    correct_score: float,
    predicted_score: float,
    correct_template: np.ndarray,
    predicted_template: np.ndarray,
) -> np.ndarray:
    """Assemble horizontal tile: raw, edges, correct tmpl, pred tmpl."""

    def upscale_gray(img):
        h, w = img.shape[:2]
        return cv2.resize(img, (w * UPSCALE_FACTOR, h * UPSCALE_FACTOR),
                          interpolation=cv2.INTER_NEAREST)

    def pad_to_size(img, target_h, target_w, pad_value=0):
        """Pad image to target size."""
        h, w = img.shape[:2]
        top = (target_h - h) // 2
        bottom = target_h - h - top
        left = (target_w - w) // 2
        right = target_w - w - left
        return cv2.copyMakeBorder(img, top, bottom, left, right,
                                  cv2.BORDER_CONSTANT, value=pad_value)

    # Upscale all components
    raw_upscaled = upscale_gray(raw_crop)
    edges_upscaled = upscale_gray(edge_crop)
    correct_upscaled = upscale_gray(correct_template)
    predicted_upscaled = upscale_gray(predicted_template)

    # Pad all to same height (max height)
    max_h = max(raw_upscaled.shape[0], edges_upscaled.shape[0],
                correct_upscaled.shape[0], predicted_upscaled.shape[0])
    raw_upscaled = pad_to_size(raw_upscaled, max_h, raw_upscaled.shape[1])
    edges_upscaled = pad_to_size(edges_upscaled, max_h, edges_upscaled.shape[1])
    correct_upscaled = pad_to_size(correct_upscaled, max_h, correct_upscaled.shape[1])
    predicted_upscaled = pad_to_size(predicted_upscaled, max_h, predicted_upscaled.shape[1])

    # Add white text overlay to each tile
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.4
    font_color = 255
    thickness = 1

    # Helper to add text to a tile
    def add_text(tile, text_lines):
        tile_with_text = tile.copy()
        for i, txt in enumerate(text_lines):
            cv2.putText(
                tile_with_text, txt,
                (5, 15 + i * 12),
                font, font_scale, font_color, thickness
            )
        return tile_with_text

    raw_labeled = add_text(raw_upscaled, [f"Raw ROI (scan {scan_id})"])
    edges_labeled = add_text(edges_upscaled, ["Canny edges"])
    correct_labeled = add_text(
        correct_upscaled,
        [f"TRUE: {correct_set}", f"Score: {correct_score:.3f}"]
    )
    predicted_labeled = add_text(
        predicted_upscaled,
        [f"PRED: {predicted_set}", f"Score: {predicted_score:.3f}"]
    )

    # Stack horizontally
    tile = np.hstack([
        raw_labeled,
        edges_labeled,
        correct_labeled,
        predicted_labeled,
    ])

    return tile


def main():
    print("[load] Loading templates and scan index...")
    failures = collect_failures()

    if not failures:
        print("No failures found in target sets {woe, blb, bro}.")
        return

    templates = load_templates(use_edge=True)

    for set_code in sorted(failures.keys()):
        fails = failures[set_code]
        print(f"\n=== {set_code.upper()} ({len(fails)} failures) ===")

        # Take up to MAX_FAILS_PER_SET
        for i, fail in enumerate(fails[:MAX_FAILS_PER_SET]):
            scan_id = fail["scan_num"]
            crop_path = fail["crop_path"]
            true_set = fail["true_set"]
            roi = fail["roi"]
            frame = fail["frame"]
            scores = fail["scores"]

            # Load the scan image
            img = cv2.imread(crop_path)
            if img is None:
                print(f"  {i+1}. scan {scan_id}: could not load image")
                continue

            # Preprocess: get grayscale crop + edge crop
            gray_roi = preprocess_roi(img, roi, use_edge=False)
            edge_roi = preprocess_roi(img, roi, use_edge=True)

            if gray_roi.size == 0 or edge_roi.size == 0:
                print(f"  {i+1}. scan {scan_id}: empty ROI after preprocessing")
                continue

            # Get scores from our collection
            pred_set = scores[0][0]
            pred_score = scores[0][1]

            # Find correct_score in scores
            correct_score = None
            for code, sc in scores:
                if code == true_set:
                    correct_score = sc
                    break

            if correct_score is None:
                correct_score = -1.0

            # Load templates
            correct_template = templates.get((true_set, frame))
            predicted_template = templates.get((pred_set, frame))

            if correct_template is None or predicted_template is None:
                print(f"  {i+1}. scan {scan_id}: missing templates")
                continue

            # Assemble tile
            tile = assemble_diagnostic_tile(
                scan_id=scan_id,
                raw_crop=gray_roi,
                edge_crop=edge_roi,
                correct_set=true_set,
                predicted_set=pred_set,
                correct_score=correct_score,
                predicted_score=pred_score,
                correct_template=correct_template,
                predicted_template=predicted_template,
            )

            # Save
            out_path = os.path.join(OUT_DIR, f"{true_set}_{scan_id}.png")
            cv2.imwrite(out_path, tile)

            delta = correct_score - pred_score
            print(f"  {i+1}. scan {scan_id}: "
                  f"TRUE: {true_set} SCORE: {correct_score:.3f} | "
                  f"PRED: {pred_set} SCORE: {pred_score:.3f} | "
                  f"delta: {delta:.3f}")
            print(f"       -> {out_path}")


if __name__ == "__main__":
    main()
