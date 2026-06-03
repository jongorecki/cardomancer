# config.py
# Central configuration for the MTG Card Sorter

import os

# --- Brand ---
# Cardomancer (a play on "cartomancy" — divination by cards). Decided
# 2026-05-07. Use this constant for user-facing copy: page title,
# navbar header, error-banner branding, About / version dialog,
# generated exports, etc. Internal identifiers (modules, repo, log
# prefixes) keep their existing technical names.
APP_NAME = "Cardomancer"

# --- Paths ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CARDS_JSON_PATH = os.path.join(SCRIPT_DIR, "default-cards-20260521210656.json")
HASH_DB_PATH = os.path.join(SCRIPT_DIR, "card_hashes.json")
PRINTINGS_MAP_PATH = os.path.join(SCRIPT_DIR, "printings_map.json")
BOUNDING_BOX_PATH = os.path.join(SCRIPT_DIR, "bounding_box.json")
STAGING_ROI_PATH = os.path.join(SCRIPT_DIR, "staging_roi.json")
# Reference frame of the empty staging platform. Used for background
# subtraction: detect_card_on_staging() diffs the live frame against
# this reference so the card (the thing that CHANGED) is the only thing
# left. Capture via test_detect_only.py (press B with an empty platform).
STAGING_BG_REF_PATH = os.path.join(SCRIPT_DIR, "staging_bg_ref.png")

# --- Hashing (v3: 256-bit per-channel phash, 3 regions) ---
CROP_SIZE = 745
PHASH_DISTANCE_THRESHOLD = 120    # scaled for 256-bit (was 40 for 64-bit)
PHASH_CLOSE_MATCH_DIFF = 40       # scaled for 256-bit (was 10 for 64-bit)

# Identity-confidence threshold for autonomy-ladder routing. Scans whose
# best-match hash distance is BELOW this value pass straight through to
# the matched bin. Scans BETWEEN this and PHASH_DISTANCE_THRESHOLD are
# "low identity confidence" — they still match a card, but uncertainly:
# the worker routes them to the sort_config's fallback bin and seeds an
# 'identity' detection-review row so the user can verify them later
# (see plans/autonomy_ladder.md). Above PHASH_DISTANCE_THRESHOLD the
# match is rejected entirely (unrecognized path, also goes to fallback).
#
# Tuned high (most cards identify confidently in normal sessions; the
# review queue should only catch genuinely uncertain ones). Lower it
# only if the queue is too sparse to be useful.
IDENTITY_LOW_CONFIDENCE_DISTANCE = 90

# --- Sorting ---
SORTING_MODES = {
    "1": "color",
    "2": "mana_value",
    "3": "set",
    "4": "price",
    "5": "type",
    "6": "custom_file",
    "7": "custom_manual",
}
SORT_CONFIGS_DIR = os.path.join(SCRIPT_DIR, "sort_configs")
SCAN_LOGS_DIR = os.path.join(SCRIPT_DIR, "scan_logs")

# Working assumption for destination bin capacity (cards). The Z-probe
# fullness check trips around this count in normal use; sort configs
# that don't specify their own `limit:` directive use this as the
# default cap that drives overflow routing decisions.
DEFAULT_BIN_CAPACITY = 300

# --- Source-bin count estimation ---
# Used by web_worker to estimate how many cards remain in the source
# bin from the difference between the current probe Z and a calibrated
# empty-bin reference Z, divided by the per-card thickness.
#
# 0.305 mm is the Wizards-published thickness of a standard unsleeved
# Magic card. Sleeved cards are ~0.45 mm; Penny / KMC sleeves ~0.40 mm.
# The Sort flow's "Pause to refill source" workflow makes the user
# explicitly aware of refills, so this estimate doesn't have to be
# perfect — a ~10% error margin is fine for "you have ~50 cards left."
CARD_THICKNESS_MM = 0.305

# Path where the empty-source-bin reference Z is persisted so it
# survives restarts. Single value: {"empty_z": <float>, "calibrated_at": "<iso>"}.
EMPTY_SOURCE_BIN_REF_PATH = os.path.join(SCRIPT_DIR, "empty_source_z.json")

# --- Excluded sets (promo/special sets to deprioritize) ---
EXCLUDED_SETS = {
    # Collector's / International / Foreign Black Border editions —
    # gold or non-standard borders, essentially never in bulk sorts.
    "30a", "lea", "leb", "fbb", "ced", "cei", "4bb", "ptc", "sum",
    # World Championship decks (wc97–wc04) — gold-bordered tournament
    # facsimiles.  Their hashes collide with the regular printings they
    # copy and will always produce wrong border/set metadata.
    "wc97", "wc98", "wc99", "wc00", "wc01", "wc02", "wc03", "wc04",
    # Mystery Booster playtest / The List reprints — cards look
    # identical to original printings; matching to mb1/mb2 produces a
    # wrong set code while the oracle text is the same.
    "cmb1", "cmb2", "mb2",
    # Un-sets (silver border) — Unglued, Unhinged, Unstable, Unsanctioned.
    # Topsy Turvy (und/29, unh/47) is a specific false-positive magnet:
    # its upside-down design hash-collides with any card scanned upside-down.
    # Unfinity is intentionally NOT excluded — user has Unfinity cards.
    "ugl", "unh", "ust", "und",
    # Unknown Event — dummy placeholders (Common Curve Filler etc.)
    "unk",
}

# --- Hardware / G-code ---
X_DETECTION_POSITION = 100.0

# --- Card canonical dimensions (pixels) ---
# These MUST match the actual dimensions of the Scryfall PNG images in
# downloaded_cards/ — the v2 hash DB was built by cropping ART_REGION from
# those PNGs, so the runtime warp has to produce images of the same size
# or the crop maps to slightly different card-space pixels and phash breaks.
# Scryfall's "png" image variant is 745x1040.
CARD_WIDTH = 745
CARD_HEIGHT = 1040

# --- Inner region for hashing (pixel coordinates on 745x1040 Scryfall images) ---
# Trims outer border and bottom half (text box, P/T, collector info).
# Keeps title + art + type line — the most visually unique content.
ART_REGION = (30, 105, 715, 520)  # (x1, y1, x2, y2) — normal/adventure/planeswalker
ART_REGION_SAGA = (350, 60, 715, 600)  # Saga: art on right side, text on left
ART_REGION_CLASS = (30, 60, 350, 600)  # Class/Case: art on left side, text on right
# Battle: art occupies roughly left 2/3 of card (through x=500) with the
# battle text/chapter strip on the right 1/3. Empirically derived from
# saturation profiling of Invasion of Tolvada. Wider than ART_REGION_CLASS
# (stops at x=350) and narrower than ART_REGION (stops at x=715).
ART_REGION_BATTLE = (30, 105, 500, 620)

# --- Hash database paths ---
HASH_DB_V2_PATH = os.path.join(SCRIPT_DIR, "card_hashes_v2.json")

# --- OCR region definitions (fractions of CARD_WIDTH x CARD_HEIGHT) ---
# Title bar: top ~12% of card, wider crop to catch full name + mana cost area
TITLE_REGION = (0.03, 0.01, 0.80, 0.12)  # (x_frac, y_frac, w_frac, h_frac)
# Collector info: bottom 6%, left 55%
COLLECTOR_REGION = (0.03, 0.92, 0.55, 0.06)

# --- Card back detection ---
CARD_BACK_REF_PATH = os.path.join(SCRIPT_DIR, "card_back_reference.png")
CARD_BACK_DISTANCE_THRESHOLD = 60  # Below this = card back detected

# --- Tesseract ---
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# --- ArUco Calibration ---
# CAMERA_X_OFFSET = camera_world_x - head_world_x
#   POSITIVE = camera is to the RIGHT of the head (higher X)
#   NEGATIVE = camera is to the LEFT of the head (lower X)
#
# Usage throughout the code (Marlin G0 X controls the head position):
#   head   world X = carriage_x
#   camera world X = carriage_x + CAMERA_X_OFFSET
#
# Calibration procedure: place a marker directly under the head, note
# where it appears in the camera frame, measure/compute the delta in mm.
# A physical tape-measure from the head tip to the camera lens center
# (signed along +X) is usually close enough as a starting value.
CAMERA_X_OFFSET = 100.0

# Maximum X sweep distance during calibration (mm)
# Set this conservatively if you don't have an X max endstop
# Physical X travel is ~800mm; leave a small safety margin.
CALIBRATION_MAX_SWEEP_X = 790.0

# Sweep speed during calibration (mm/min) — keep slow for reliability
CALIBRATION_SWEEP_FEEDRATE = 2000

# ArUco marker ID ranges
# Source bins: IDs 0-9, Destination bins: IDs 10-49
ARUCO_SOURCE_ID_MIN = 0
ARUCO_SOURCE_ID_MAX = 9
ARUCO_DEST_ID_MIN = 10
ARUCO_DEST_ID_MAX = 49
