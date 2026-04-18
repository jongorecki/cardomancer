#!/usr/bin/env python
"""
generate_staging_mat.py
=======================
Generate a printable staging mat with ArUco markers at fixed positions.

The mat has 4 ArUco markers (IDs 100-103) placed so that the area
between them frames exactly where a card should be placed. When the
camera sees these 4 markers, it can compute a precise perspective
transform to extract the card area.

Layout:
    [100]                [101]
         +------------+
         |            |
         |   CARD     |
         |   AREA     |
         |            |
         +------------+
    [103]                [102]

The card area is 63mm x 88mm (standard MTG card).
The markers are placed with some margin outside the card area.

Usage:
    python generate_staging_mat.py
    # Prints staging_mat.png — print at 100% scale on letter/A4 paper

Tape the printed mat to the staging platform with the card drop
zone centered on the card area outlined on the mat.
"""

import cv2
import numpy as np

# --- Configuration ---
# ArUco marker IDs for the staging mat corners
STAGING_MARKER_IDS = [100, 101, 102, 103]  # TL, TR, BR, BL

# Physical dimensions (mm)
CARD_WIDTH_MM = 63.0    # MTG card width
CARD_HEIGHT_MM = 88.0   # MTG card height
MARKER_SIZE_MM = 15.0   # ArUco marker size
MARGIN_MM = 10.0        # Gap between card edge and marker edge

# DPI for printing (300 DPI = good quality)
DPI = 300

def mm_to_px(mm):
    """Convert millimeters to pixels at the configured DPI."""
    return int(round(mm * DPI / 25.4))

def generate_staging_mat(output_path="staging_mat.png"):
    """Generate the staging mat image."""
    from cv2 import aruco

    # Calculate layout
    marker_px = mm_to_px(MARKER_SIZE_MM)
    margin_px = mm_to_px(MARGIN_MM)
    card_w_px = mm_to_px(CARD_WIDTH_MM)
    card_h_px = mm_to_px(CARD_HEIGHT_MM)

    # Total mat size: markers + margins + card area
    # Each side: marker + margin + card edge
    mat_w = 2 * marker_px + 2 * margin_px + card_w_px + 2 * margin_px
    mat_h = 2 * marker_px + 2 * margin_px + card_h_px + 2 * margin_px

    # Create white mat
    mat = np.ones((mat_h, mat_w), dtype=np.uint8) * 255

    # Card area position (centered)
    card_x1 = (mat_w - card_w_px) // 2
    card_y1 = (mat_h - card_h_px) // 2
    card_x2 = card_x1 + card_w_px
    card_y2 = card_y1 + card_h_px

    # Draw card outline (thin gray rectangle)
    cv2.rectangle(mat, (card_x1, card_y1), (card_x2, card_y2), 180, 2)

    # Draw "PLACE CARD HERE" text
    font = cv2.FONT_HERSHEY_SIMPLEX
    text = "PLACE CARD HERE"
    text_size = cv2.getTextSize(text, font, 0.5, 1)[0]
    text_x = card_x1 + (card_w_px - text_size[0]) // 2
    text_y = card_y1 + card_h_px + margin_px
    cv2.putText(mat, text, (text_x, text_y), font, 0.5, 128, 1)

    # Generate and place ArUco markers
    aruco_dict = aruco.getPredefinedDictionary(aruco.DICT_ARUCO_ORIGINAL)

    # Marker positions: corners of the mat, inset by a small border
    border_px = mm_to_px(3)  # small border from paper edge
    positions = [
        (border_px, border_px),                              # TL - ID 100
        (mat_w - border_px - marker_px, border_px),          # TR - ID 101
        (mat_w - border_px - marker_px, mat_h - border_px - marker_px),  # BR - ID 102
        (border_px, mat_h - border_px - marker_px),          # BL - ID 103
    ]

    for marker_id, (mx, my) in zip(STAGING_MARKER_IDS, positions):
        marker_img = aruco.generateImageMarker(aruco_dict, marker_id, marker_px)
        mat[my:my+marker_px, mx:mx+marker_px] = marker_img

    # Add marker ID labels
    for marker_id, (mx, my) in zip(STAGING_MARKER_IDS, positions):
        label = f"ID:{marker_id}"
        lx = mx + marker_px // 2 - 20
        ly = my + marker_px + 15
        if ly > mat_h - 10:
            ly = my - 5
        cv2.putText(mat, label, (lx, ly), font, 0.35, 128, 1)

    # Save
    cv2.imwrite(output_path, mat)
    print(f"Staging mat saved to {output_path}")
    print(f"  Mat size: {mat_w}x{mat_h} px ({mat_w*25.4/DPI:.0f}x{mat_h*25.4/DPI:.0f} mm)")
    print(f"  Card area: {card_w_px}x{card_h_px} px ({CARD_WIDTH_MM}x{CARD_HEIGHT_MM} mm)")
    print(f"  Marker size: {marker_px}x{marker_px} px ({MARKER_SIZE_MM} mm)")
    print(f"  Print at {DPI} DPI (100% scale, no fit-to-page)")
    print(f"  ArUco IDs: {STAGING_MARKER_IDS}")
    return output_path


if __name__ == '__main__':
    generate_staging_mat()
