"""
probe_foil_pairs.py
-------------------
Walks a scan session's paired crops (card_crops/ + card_crops_b/) and
reports per-card signals that a foil detector would key off of:

  - mean_abs_delta:   mean(|A - B|) across BGR.  If this is ~0 the pair
                      is effectively identical (capture failed or offset
                      is too small).  Non-foils sit at some baseline from
                      natural lighting/registration jitter; foils sit
                      markedly higher.
  - hue_shift:        mean(|H_A - H_B|) in HSV hue degrees (0-180).
                      Foils shift hue under viewing angle; non-foils
                      don't.
  - sat_delta:        mean(|S_A - S_B|).  Foils also shift saturation
                      with angle.
  - bright_shift_px:  how many pixels move into/out of the "bright
                      specular" band (V > 230) between the two frames.
                      Foil glints translate across the surface when the
                      viewing angle changes; matte cards don't.
  - sharp_a / sharp_b: variance of Laplacian on each crop.  Useful to
                      spot pairs where the B frame was motion-blurred
                      (vibration not settled).

Outputs:
  - A CSV of all signals to <session>/foil_probe.csv
  - A side-by-side diff PNG per card to <session>/foil_probe/ (A | B | |A-B| heatmap)
  - A printed ranked table on stdout (most foil-like first by hue_shift)

Usage:
  python probe_foil_pairs.py                       # latest session
  python probe_foil_pairs.py --session <name>      # specific session dir
  python probe_foil_pairs.py --no-images           # CSV + stdout only
"""

from __future__ import annotations
import argparse
import csv
import os
import sys
from pathlib import Path

import cv2
import numpy as np

SCAN_LOGS = Path(__file__).parent / "scan_logs"


def latest_session() -> Path:
    sessions = [p for p in SCAN_LOGS.iterdir() if p.is_dir() and p.name.startswith("session_")]
    if not sessions:
        raise SystemExit(f"No sessions under {SCAN_LOGS}")
    return max(sessions, key=lambda p: p.stat().st_mtime)


def laplacian_sharpness(img: np.ndarray) -> float:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def compute_signals(a: np.ndarray, b: np.ndarray) -> dict:
    # Align sizes (should already match — CARD_WIDTH x CARD_HEIGHT)
    if a.shape != b.shape:
        h = min(a.shape[0], b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a = a[:h, :w]
        b = b[:h, :w]

    # BGR deltas
    diff = cv2.absdiff(a, b)
    mean_abs_delta = float(diff.mean())

    # HSV deltas
    hsv_a = cv2.cvtColor(a, cv2.COLOR_BGR2HSV)
    hsv_b = cv2.cvtColor(b, cv2.COLOR_BGR2HSV)
    # Hue wraps 0-180 in OpenCV; compute circular distance.
    dh = cv2.absdiff(hsv_a[..., 0], hsv_b[..., 0]).astype(np.int16)
    dh = np.minimum(dh, 180 - dh)
    hue_shift = float(dh.mean())
    sat_delta = float(cv2.absdiff(hsv_a[..., 1], hsv_b[..., 1]).mean())

    # Specular shift: pixels in the very-bright band that moved.
    V_THRESH = 230
    bright_a = hsv_a[..., 2] > V_THRESH
    bright_b = hsv_b[..., 2] > V_THRESH
    bright_shift_px = int(np.logical_xor(bright_a, bright_b).sum())

    return {
        "mean_abs_delta": mean_abs_delta,
        "hue_shift": hue_shift,
        "sat_delta": sat_delta,
        "bright_shift_px": bright_shift_px,
        "sharp_a": laplacian_sharpness(a),
        "sharp_b": laplacian_sharpness(b),
    }


def save_debug_image(a: np.ndarray, b: np.ndarray, out_path: Path) -> None:
    """A | B | |A-B| heatmap side-by-side."""
    if a.shape != b.shape:
        h = min(a.shape[0], b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a = a[:h, :w]
        b = b[:h, :w]
    diff = cv2.absdiff(a, b)
    # Scale diff for visibility (foil signal is usually < 30/255 per pixel).
    heat = cv2.applyColorMap(
        np.clip(diff.mean(axis=2) * 4, 0, 255).astype(np.uint8),
        cv2.COLORMAP_INFERNO,
    )
    triptych = np.hstack([a, b, heat])
    # Downscale for smaller files (images are 745x1040 each * 3).
    h = triptych.shape[0]
    if h > 700:
        scale = 700 / h
        triptych = cv2.resize(triptych, (int(triptych.shape[1] * scale), 700))
    cv2.imwrite(str(out_path), triptych, [cv2.IMWRITE_JPEG_QUALITY, 85])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", type=str, default=None,
                    help="Session dir name (default: most recent)")
    ap.add_argument("--no-images", action="store_true",
                    help="Skip writing side-by-side debug images")
    args = ap.parse_args()

    session = (SCAN_LOGS / args.session) if args.session else latest_session()
    if not session.exists():
        raise SystemExit(f"Session not found: {session}")

    crops_a = session / "card_crops"
    crops_b = session / "card_crops_b"
    if not crops_b.exists():
        raise SystemExit(
            f"No card_crops_b/ in {session.name} — foil pair capture did "
            f"not run for this session (or the feature wasn't deployed yet)."
        )

    pair_names = sorted(
        p.name for p in crops_a.iterdir()
        if p.suffix == ".jpg" and (crops_b / p.name).exists()
    )
    if not pair_names:
        raise SystemExit(f"No matching pairs found in {session.name}")

    missing_b = sum(1 for p in crops_a.iterdir()
                    if p.suffix == ".jpg" and not (crops_b / p.name).exists())

    print(f"[probe] Session: {session.name}")
    print(f"[probe] Paired crops: {len(pair_names)}  "
          f"(unpaired primary-only: {missing_b})")
    print()

    debug_dir = session / "foil_probe"
    if not args.no_images:
        debug_dir.mkdir(exist_ok=True)

    rows = []
    for name in pair_names:
        a = cv2.imread(str(crops_a / name))
        b = cv2.imread(str(crops_b / name))
        if a is None or b is None:
            print(f"[probe] skip {name} (read failed)")
            continue
        sig = compute_signals(a, b)
        sig["card"] = name
        rows.append(sig)
        if not args.no_images:
            save_debug_image(a, b, debug_dir / name.replace(".jpg", "_pair.jpg"))

    if not rows:
        raise SystemExit("No pairs could be analyzed.")

    # Ranked table by hue_shift desc — foil candidates float to the top.
    rows.sort(key=lambda r: r["hue_shift"], reverse=True)

    print(f"{'card':<18}  {'mean_abs':>9}  {'hue':>6}  {'sat':>6}  "
          f"{'bright_dpx':>10}  {'sharp_a':>8}  {'sharp_b':>8}")
    print("-" * 78)
    for r in rows:
        print(f"{r['card']:<18}  {r['mean_abs_delta']:>9.2f}  "
              f"{r['hue_shift']:>6.2f}  {r['sat_delta']:>6.2f}  "
              f"{r['bright_shift_px']:>10d}  "
              f"{r['sharp_a']:>8.1f}  {r['sharp_b']:>8.1f}")

    # Summary stats
    n = len(rows)
    means = {k: sum(r[k] for r in rows) / n
             for k in ("mean_abs_delta", "hue_shift", "sat_delta",
                       "bright_shift_px")}
    print("-" * 78)
    print(f"{'mean':<18}  {means['mean_abs_delta']:>9.2f}  "
          f"{means['hue_shift']:>6.2f}  {means['sat_delta']:>6.2f}  "
          f"{means['bright_shift_px']:>10.0f}")

    # CSV
    csv_path = session / "foil_probe.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "card", "mean_abs_delta", "hue_shift", "sat_delta",
            "bright_shift_px", "sharp_a", "sharp_b"])
        w.writeheader()
        w.writerows(rows)
    print()
    print(f"[probe] CSV:   {csv_path}")
    if not args.no_images:
        print(f"[probe] Pair images: {debug_dir}")

    # Heuristic hint for interpretation.
    print()
    print("Interpretation:")
    print("  - If mean_abs_delta < 1.0 on every card, the pair capture is")
    print("    essentially the same image (offset too small, or B frame")
    print("    came from before the X move settled).")
    print("  - Non-foil cards typically sit at mean_abs_delta ~2-5 from")
    print("    natural lighting/registration jitter.")
    print("  - Foils should show hue_shift > ~1.0 and a much higher")
    print("    bright_shift_px (hundreds-to-thousands).  Rank order in")
    print("    the table above should roughly put foils at the top.")


if __name__ == "__main__":
    main()
