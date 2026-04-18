#!/usr/bin/env python3
"""
Generate ArUco markers for the MTG card sorter bin calibration system.

Uses DICT_4X4_50 (same as web_calibration.py).
Marker ID mapping:
  0-9   = Source bins
  10-48 = Destination bins
  49    = Staging platform

Each marker is generated at MTG card size (63x88mm) with:
  - White background filling the full card area
  - Black ArUco marker centered on the card
  - ID number and type label printed below the marker
  - Rounded-corner card outline as a cutting guide

Output: Individual PNGs + printable 8.5x11 sheets with cut lines.

Usage:
  python generate_aruco_markers.py                    # IDs 0-15 + staging (default)
  python generate_aruco_markers.py 0 20               # IDs 0-20 + staging
  python generate_aruco_markers.py --no-staging 0 10  # IDs 0-10, no staging
"""

import os
import sys
import cv2
import numpy as np

# Match web_calibration.py
ARUCO_DICT_TYPE = cv2.aruco.DICT_4X4_50
MARKER_SIZE_MM = 40  # ArUco marker square size

# MTG card size in mm
CARD_WIDTH_MM = 63
CARD_HEIGHT_MM = 88
CARD_CORNER_RADIUS_MM = 3  # MTG cards have ~3mm corner radius

# US Letter paper
PAPER_WIDTH_MM = 215.9   # 8.5 inches
PAPER_HEIGHT_MM = 279.4  # 11 inches
PAPER_MARGIN_MM = 10     # Safe margin for most printers

# Output resolution: 300 DPI
DPI = 300
MM_PER_INCH = 25.4

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'aruco_markers')


def mm_to_px(mm):
    return int(round(mm * DPI / MM_PER_INCH))


def draw_rounded_rect(img, x, y, w, h, radius, color, thickness):
    """Draw a rounded rectangle outline."""
    r = radius
    # Straight edges (inset by radius)
    cv2.line(img, (x + r, y), (x + w - r, y), color, thickness)           # top
    cv2.line(img, (x + r, y + h), (x + w - r, y + h), color, thickness)   # bottom
    cv2.line(img, (x, y + r), (x, y + h - r), color, thickness)           # left
    cv2.line(img, (x + w, y + r), (x + w, y + h - r), color, thickness)   # right
    # Corners
    cv2.ellipse(img, (x + r, y + r), (r, r), 180, 0, 90, color, thickness)          # top-left
    cv2.ellipse(img, (x + w - r, y + r), (r, r), 270, 0, 90, color, thickness)      # top-right
    cv2.ellipse(img, (x + w - r, y + h - r), (r, r), 0, 0, 90, color, thickness)    # bottom-right
    cv2.ellipse(img, (x + r, y + h - r), (r, r), 90, 0, 90, color, thickness)       # bottom-left


def get_marker_label(marker_id):
    """Return human-readable label for a marker ID."""
    if marker_id == 49:
        return "STAGING"
    elif marker_id < 10:
        return f"SOURCE {marker_id}"
    else:
        return f"BIN {marker_id - 9}"


def generate_marker(marker_id, dict_type=ARUCO_DICT_TYPE):
    """Generate a single ArUco marker image at card size with rounded-corner outline."""
    aruco_dict = cv2.aruco.getPredefinedDictionary(dict_type)

    card_w = mm_to_px(CARD_WIDTH_MM)
    card_h = mm_to_px(CARD_HEIGHT_MM)
    marker_px = mm_to_px(MARKER_SIZE_MM)
    corner_r = mm_to_px(CARD_CORNER_RADIUS_MM)

    # Generate the raw marker (black and white grid)
    marker_img = cv2.aruco.generateImageMarker(aruco_dict, marker_id, marker_px)

    # Create white card-sized canvas
    card = np.ones((card_h, card_w), dtype=np.uint8) * 255

    # Center the marker on the card (shifted up to leave room for labels)
    x_offset = (card_w - marker_px) // 2
    y_offset = (card_h - marker_px) // 2 - mm_to_px(8)
    if y_offset < mm_to_px(4):
        y_offset = mm_to_px(4)

    card[y_offset:y_offset + marker_px, x_offset:x_offset + marker_px] = marker_img

    # Add ID label below marker
    label = f"ID {marker_id}"
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.7
    thickness = 2
    (text_w, text_h), _ = cv2.getTextSize(label, font, font_scale, thickness)
    text_x = (card_w - text_w) // 2
    text_y = y_offset + marker_px + mm_to_px(6)
    cv2.putText(card, label, (text_x, text_y), font, font_scale, 0, thickness)

    # Add type label below ID
    type_label = get_marker_label(marker_id)
    font_scale2 = 0.5
    thickness2 = 1
    (tw2, th2), _ = cv2.getTextSize(type_label, font, font_scale2, thickness2)
    tx2 = (card_w - tw2) // 2
    ty2 = text_y + mm_to_px(5)
    cv2.putText(card, type_label, (tx2, ty2), font, font_scale2, 80, thickness2)

    # Rounded-corner card outline (cutting guide) — solid black, thick
    outline_thickness = 5
    inset = outline_thickness // 2 + 1
    draw_rounded_rect(card, inset, inset,
                      card_w - inset * 2 - 1, card_h - inset * 2 - 1,
                      corner_r, 0, outline_thickness)

    return card


def build_print_sheets(marker_ids):
    """
    Arrange markers onto 8.5x11 sheets, fitting as many as possible per sheet.

    Portrait cards (63x88mm) on portrait paper:
      3 across x 2 down = 6 per sheet (with comfortable margins)

    Returns list of sheet images.
    """
    paper_w = mm_to_px(PAPER_WIDTH_MM)
    paper_h = mm_to_px(PAPER_HEIGHT_MM)
    margin = mm_to_px(PAPER_MARGIN_MM)
    card_w = mm_to_px(CARD_WIDTH_MM)
    card_h = mm_to_px(CARD_HEIGHT_MM)

    # Calculate grid layout
    usable_w = paper_w - 2 * margin
    usable_h = paper_h - 2 * margin

    cols = usable_w // card_w       # 3
    rows = usable_h // card_h       # 2
    per_sheet = cols * rows         # 6

    # Center the grid on the page
    grid_w = cols * card_w
    grid_h = rows * card_h
    x_start = (paper_w - grid_w) // 2
    y_start = (paper_h - grid_h) // 2

    # Gap between cards (spread evenly)
    x_gap = (usable_w - cols * card_w) // max(cols - 1, 1) if cols > 1 else 0
    y_gap = (usable_h - rows * card_h) // max(rows - 1, 1) if rows > 1 else 0

    # Recalculate start to center with gaps
    total_grid_w = cols * card_w + (cols - 1) * x_gap
    total_grid_h = rows * card_h + (rows - 1) * y_gap
    x_start = (paper_w - total_grid_w) // 2
    y_start = (paper_h - total_grid_h) // 2

    sheets = []
    for page_start in range(0, len(marker_ids), per_sheet):
        page_ids = marker_ids[page_start:page_start + per_sheet]

        # White paper
        sheet = np.ones((paper_h, paper_w), dtype=np.uint8) * 255

        for idx, mid in enumerate(page_ids):
            r, c = divmod(idx, cols)
            x = x_start + c * (card_w + x_gap)
            y = y_start + r * (card_h + y_gap)

            marker = generate_marker(mid)
            sheet[y:y + card_h, x:x + card_w] = marker

        # Add page info at bottom
        info = f"ArUco DICT_4X4_50 | {CARD_WIDTH_MM}x{CARD_HEIGHT_MM}mm (MTG card size) | Cut along rounded outlines"
        font = cv2.FONT_HERSHEY_SIMPLEX
        (tw, th), _ = cv2.getTextSize(info, font, 0.4, 1)
        cv2.putText(sheet, info, ((paper_w - tw) // 2, paper_h - mm_to_px(3)),
                    font, 0.4, 140, 1)

        # Page number
        page_num = len(sheets) + 1
        total_pages = (len(marker_ids) + per_sheet - 1) // per_sheet
        page_label = f"Page {page_num}/{total_pages}"
        (pw, _), _ = cv2.getTextSize(page_label, font, 0.4, 1)
        cv2.putText(sheet, page_label, (paper_w - margin - pw, mm_to_px(6)),
                    font, 0.4, 140, 1)

        sheets.append(sheet)

    return sheets


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Parse arguments
    include_staging = True
    start_id = 0
    end_id = 15  # Default: 0-15

    args = sys.argv[1:]
    if '--no-staging' in args:
        include_staging = False
        args.remove('--no-staging')

    if len(args) >= 2:
        start_id = int(args[0])
        end_id = int(args[1])
    elif len(args) == 1:
        end_id = int(args[0])

    # Build list of marker IDs to generate
    marker_ids = list(range(start_id, end_id + 1))
    if include_staging and 49 not in marker_ids:
        marker_ids.append(49)

    print(f"Generating {len(marker_ids)} ArUco markers")
    print(f"  IDs: {marker_ids[0]}-{marker_ids[-2] if len(marker_ids) > 1 else marker_ids[0]}"
          + (f" + staging (49)" if include_staging else ""))
    print(f"  Dictionary: DICT_4X4_50")
    print(f"  Card size: {CARD_WIDTH_MM}x{CARD_HEIGHT_MM}mm ({DPI} DPI)")
    print(f"  Marker size: {MARKER_SIZE_MM}mm")
    print(f"  Output: {OUTPUT_DIR}/")
    print()

    # Generate individual marker PNGs
    for mid in marker_ids:
        marker = generate_marker(mid)
        if mid == 49:
            label = "staging"
        elif mid < 10:
            label = f"source_{mid}"
        else:
            label = f"dest_{mid}"
        filename = f"marker_{label}_id{mid}.png"
        path = os.path.join(OUTPUT_DIR, filename)
        cv2.imwrite(path, marker)
        print(f"  {filename}")

    # Generate printable 8.5x11 sheets
    print(f"\nBuilding print sheets (8.5 x 11\", 6 markers per sheet)...")
    sheets = build_print_sheets(marker_ids)

    for i, sheet in enumerate(sheets):
        sheet_path = os.path.join(OUTPUT_DIR, f'print_sheet_{i+1}.png')
        cv2.imwrite(sheet_path, sheet)
        # Figure out which IDs are on this sheet
        per_sheet = 6
        page_ids = marker_ids[i * per_sheet:(i + 1) * per_sheet]
        ids_str = ', '.join(str(x) for x in page_ids)
        print(f"  print_sheet_{i+1}.png  (IDs: {ids_str})")

    print(f"\n{len(sheets)} sheets total — print at 100% scale (no scaling/fit-to-page).")
    print("Cut along the rounded outlines.")
    print(f"\nDone! {len(marker_ids)} markers generated.")


if __name__ == '__main__':
    main()
