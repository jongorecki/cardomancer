# frame_era_classifier.py
# ---------------------------------------------------------------------------
# Standalone frame-era + border-color classifier for a rectified
# 745x1040 card scan.
#
# Scryfall assigns every printing:
#   frame          -> "1993" | "1997" | "2003" | "2015" | "future"
#   border_color   -> "black" | "white" | "borderless" | "silver" | "gold"
#
# We collapse to two orthogonal axes useful for printing disambiguation:
#   frame_era:    "retro" | "2003" | "2015" | "borderless"
#   border_color: "black" | "white" | "borderless"  (None when era=borderless)
#
# "retro" combines Scryfall's "1993" and "1997" frames: at 745x1040 their
# title-bar features overlap heavily, and downstream (icon ROI selection)
# treats them the same, so we do not try to split them.
#
# Not to be confused with frame_detect.py, which is the candidate-list
# phash resolver used as Stage 1 of the printing-disambiguation cascade.
# This classifier runs without any candidate list — it answers "what
# does this card physically look like" and is intended as its own
# feature (used both to constrain the set-symbol ROI and to feed future
# foil / condition / border-variant disambiguation).
#
# Decision tree (trained/validated on tests/fixtures/frame_classifier/).
# ---------------------------------------------------------------------------

from typing import Dict, Optional

import cv2
import numpy as np

WIDTH = 745
HEIGHT = 1040

FRAME_RETRO = "retro"
FRAME_2003 = "2003"
FRAME_2015 = "2015"
FRAME_BORDERLESS = "borderless"

BORDER_BLACK = "black"
BORDER_WHITE = "white"
BORDER_BORDERLESS = "borderless"


def _ensure_shape(img: np.ndarray) -> np.ndarray:
    if img.shape[:2] != (HEIGHT, WIDTH):
        return cv2.resize(img, (WIDTH, HEIGHT))
    return img


def compute_features(card_img: np.ndarray) -> Dict[str, float]:
    img = _ensure_shape(card_img)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV) if img.ndim == 3 else None

    H, W = gray.shape

    # Outer 6-px ring (border-color proxy).
    ring = np.concatenate([
        gray[0:6, :].flatten(),
        gray[H - 6:H, :].flatten(),
        gray[:, 0:6].flatten(),
        gray[:, W - 6:W].flatten(),
    ])
    border_mean = float(ring.mean())

    # Mid-ring: inside the edge antialiasing but still within the solid
    # border band on framed cards. Borderless cards push art into this
    # band, so std is high; framed cards keep std near 0-3.
    mid_ring = np.concatenate([
        gray[10:22, 40:W - 40].flatten(),
        gray[H - 22:H - 10, 40:W - 40].flatten(),
        gray[40:H - 40, 10:22].flatten(),
        gray[40:H - 40, W - 22:W - 10].flatten(),
    ])
    mid_std = float(mid_ring.std())

    # Strongest horizontal gradient near the art-box top.
    sobel_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    best_y, best_str = 0, 0.0
    for y in range(90, 145):
        s = float(np.abs(sobel_y[y, 50:695]).mean())
        if s > best_str:
            best_str = s
            best_y = y

    # Bottom-left rarity-line region. 2015 / borderless have a tidy
    # collector strip (low contrast); old frames have art pushed to the
    # bottom (high contrast).
    bl_roi = gray[963:988, 14:47]
    rar_contrast = float(bl_roi.max()) - float(bl_roi.min())

    # Title-bar region. In V channel, 1993/1997 beveled bronze banners
    # read dark (<140 typical); 2003/2015 colored parchment bars read
    # bright (>140). In S channel, 1993/1997 bronze is highly saturated;
    # 2015 near-white is unsaturated.
    if hsv is not None:
        title_val = float(np.mean(hsv[42:78, 60:680, 2]))
        title_sat = float(np.mean(hsv[42:78, 60:680, 1]))
    else:
        title_val = float(gray[42:78, 60:680].mean())
        title_sat = 0.0

    return {
        "border_mean": border_mean,
        "mid_std": mid_std,
        "art_top_y": float(best_y),
        "art_top_str": best_str,
        "rar_contrast": rar_contrast,
        "title_val": title_val,
        "title_sat": title_sat,
    }


def _classify_era(f: Dict[str, float]) -> str:
    # Borderless: art extends into the mid-ring band where framed cards
    # have a flat solid border. Catches ~70% of borderless (the rest have
    # a dark/uniform edge that looks framed and fall through).
    if f["mid_std"] > 10:
        return FRAME_BORDERLESS

    # 2015: tidy collector strip → rar_contrast near 0. 2003 has art at
    # the bottom (rar_contrast > 30 almost always), 1993/1997 even more so.
    if f["rar_contrast"] < 30:
        return FRAME_2015

    # 2003: art box pushed down past y=107; retro frames keep art above 105.
    # Boundary override: Alpha/CE retro cards measure art_top_y 107-111
    # (border texture confuses the Sobel peak). Their title bars are
    # bronze (title_sat >= 60) and dark (title_val < 140), which
    # separates cleanly from 2003's parchment banner (title_sat < 70,
    # title_val >= 160).
    if f["art_top_y"] >= 107:
        if f["title_sat"] >= 60 and f["title_val"] < 140:
            return FRAME_RETRO
        return FRAME_2003

    return FRAME_RETRO


def _classify_border(f: Dict[str, float], era: str) -> Optional[str]:
    if era == FRAME_BORDERLESS or f["mid_std"] > 10:
        return BORDER_BORDERLESS
    if f["border_mean"] >= 180:
        return BORDER_WHITE
    return BORDER_BLACK


def classify(card_img: np.ndarray) -> Dict[str, object]:
    """
    Classify a rectified 745x1040 card scan. Returns:
      {
        "frame_era":    "retro"|"2003"|"2015"|"borderless",
        "border_color": "black"|"white"|"borderless",
        "features":     { feature_name: float }  # raw signals, for logging
      }
    """
    f = compute_features(card_img)
    era = _classify_era(f)
    border = _classify_border(f, era)
    return {
        "frame_era": era,
        "border_color": border,
        "features": f,
    }
