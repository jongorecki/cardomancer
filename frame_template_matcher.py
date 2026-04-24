# frame_template_matcher.py
# ---------------------------------------------------------------------------
# Template-matching frame classifier. Averages the bottom half of many
# real scans per (frame_era, color) group to build reference templates,
# then scores a new scan against each template via normalized correlation.
#
# Why bottom half + averaging:
#   - Top half varies wildly (art + title text), but the bottom half is
#     mostly frame structure: text box, P/T box, collector/set strip.
#   - Averaging across many cards cancels out card-specific rules text
#     and art bleed, leaving the physical frame design.
#   - Templates built from real scans domain-adapt naturally — no need
#     to hand-tune around scanner lighting/glare/platform bleed that
#     broke the scalar-feature classifier.
#
# Color axis matters most for retro (1993/1997) where the frame color
# varies dramatically (white cards are cream, red cards are red-tinted
# etc.). 2003/2015 text boxes are grayer and less color-dependent, but
# we split anyway for consistency.
#
# Usage:
#   matcher = FrameTemplateMatcher.from_scan_logs()  # builds templates
#   result = matcher.classify(card_img)
#     -> {"frame_era": "retro"|"2003"|"2015"|"borderless",
#         "color": "W"|"U"|"B"|"R"|"G"|"A"|"L"|"M",
#         "score": float, "runner_up": float}
# ---------------------------------------------------------------------------

import csv
import glob
import os
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

WIDTH = 745
HEIGHT = 1040

# Bottom-half region used for matching. y=520..1040 captures text box,
# P/T box, collector strip, bottom border. Averaged across many cards
# this cancels out rules-text variance and leaves frame structure.
BOTTOM_Y0 = 520
BOTTOM_Y1 = 1040

FRAME_RETRO = "retro"
FRAME_2003 = "2003"
FRAME_2015 = "2015"
FRAME_BORDERLESS = "borderless"

# Color categories. Lands and artifacts break out separately from the
# five colors because their frames look distinct. Multicolor cards get
# a gold frame (retro) or hybrid frame (modern) — one class covers both.
COLOR_W = "W"
COLOR_U = "U"
COLOR_B = "B"
COLOR_R = "R"
COLOR_G = "G"
COLOR_A = "A"   # artifact / colorless non-land
COLOR_L = "L"   # land
COLOR_M = "M"   # multicolor (2+ colors)


def card_color_category(card: dict) -> str:
    type_line = card.get("type_line", "") or ""
    if "Land" in type_line:
        return COLOR_L
    colors = card.get("colors") or []
    # Double-faced cards put colors on card_faces.
    if not colors:
        faces = card.get("card_faces") or []
        if faces:
            colors = faces[0].get("colors") or []
    if len(colors) == 0:
        return COLOR_A
    if len(colors) >= 2:
        return COLOR_M
    return colors[0]


def card_frame_era(card: dict) -> Optional[str]:
    bc = card.get("border_color", "")
    effects = card.get("frame_effects") or []
    if bc == "borderless" or "borderless" in effects:
        return FRAME_BORDERLESS
    fr = card.get("frame", "")
    if fr in ("1993", "1997"):
        return FRAME_RETRO
    if fr == "2003":
        return FRAME_2003
    if fr == "2015":
        return FRAME_2015
    return None


def _bottom_half(img: np.ndarray, grayscale: bool = True) -> np.ndarray:
    if img.shape[:2] != (HEIGHT, WIDTH):
        img = cv2.resize(img, (WIDTH, HEIGHT))
    crop = img[BOTTOM_Y0:BOTTOM_Y1, :]
    if grayscale and crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return crop.astype(np.float32)


def _normalize(img: np.ndarray) -> np.ndarray:
    """Zero-mean unit-std. Cancels absolute brightness differences."""
    m = img.mean()
    s = img.std() + 1e-6
    return (img - m) / s


class FrameTemplateMatcher:
    def __init__(self, templates: Dict[Tuple[str, str], np.ndarray]):
        # templates: (era, color) -> averaged bottom half (float32, normalized)
        self.templates = templates

    @classmethod
    def from_scan_dir(cls, scan_logs_dir: str, card_index: dict,
                      holdout_ratio: float = 0.0) -> Tuple["FrameTemplateMatcher", List[dict]]:
        """
        Build templates from all scans under scan_logs_dir.
        :param card_index: (set_lower, collector_number) -> card dict.
        :param holdout_ratio: if > 0, reserves that fraction of scans
            per (era, color) group for validation. Returns (matcher, holdout).
        """
        rows_by_group = defaultdict(list)
        for session in sorted(glob.glob(os.path.join(scan_logs_dir, "session_*"))):
            csv_path = os.path.join(session, "scans.csv")
            if not os.path.exists(csv_path):
                continue
            with open(csv_path, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("recognized", "").lower() != "true":
                        continue
                    card = card_index.get(
                        (r.get("set", "").lower(), r.get("collector_number", ""))
                    )
                    if not card:
                        continue
                    era = card_frame_era(card)
                    if era is None:
                        continue
                    color = card_color_category(card)
                    crop = os.path.join(
                        session, "card_crops",
                        f"card_{int(r['scan_num']):04d}.jpg",
                    )
                    if not os.path.exists(crop):
                        continue
                    rows_by_group[(era, color)].append({
                        "path": crop, "card": card,
                        "name": r.get("name", ""),
                        "set": r.get("set", ""),
                    })

        holdout = []
        templates: Dict[Tuple[str, str], np.ndarray] = {}
        rng = np.random.default_rng(seed=0)
        for key, rows in rows_by_group.items():
            if holdout_ratio > 0 and len(rows) >= 5:
                idx = rng.permutation(len(rows))
                n_hold = max(1, int(len(rows) * holdout_ratio))
                hold_idx = set(idx[:n_hold].tolist())
                train_rows = [r for i, r in enumerate(rows) if i not in hold_idx]
                for i, r in enumerate(rows):
                    if i in hold_idx:
                        r["truth_era"] = key[0]
                        r["truth_color"] = key[1]
                        holdout.append(r)
            else:
                train_rows = rows

            if len(train_rows) == 0:
                continue

            acc = None
            count = 0
            for r in train_rows:
                img = cv2.imread(r["path"])
                if img is None:
                    continue
                bh = _bottom_half(img)
                if acc is None:
                    acc = np.zeros_like(bh)
                acc += bh
                count += 1
            if count == 0:
                continue
            avg = acc / count
            templates[key] = _normalize(avg)

        return cls(templates), holdout

    def classify(self, card_img: np.ndarray,
                 color: Optional[str] = None) -> dict:
        """
        :param color: if provided (from the pHash-identified card), only
            score against templates of that color. This is the intended
            cascade use: card identity resolved first, frame era picked
            from the 4 same-color templates. Without a color filter the
            matcher picks the best (era, color) jointly but color is a
            weaker signal from the bottom half alone.
        """
        query = _normalize(_bottom_half(card_img))
        best: Tuple[Optional[Tuple[str, str]], float] = (None, -1e9)
        second = -1e9
        scores: Dict[Tuple[str, str], float] = {}
        for key, tmpl in self.templates.items():
            if color is not None and key[1] != color:
                continue
            if tmpl.shape != query.shape:
                continue
            # Mean elementwise product of two zero-mean unit-std signals
            # ~ Pearson correlation.
            s = float(np.mean(tmpl * query))
            scores[key] = s
            if s > best[1]:
                second = best[1]
                best = (key, s)
            elif s > second:
                second = s
        key, score = best
        if key is None:
            return {"frame_era": None, "color": None,
                    "score": 0.0, "runner_up": 0.0, "scores": scores}
        return {
            "frame_era": key[0], "color": key[1],
            "score": score, "runner_up": second, "scores": scores,
        }
