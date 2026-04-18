# test_sorting_live.py
# ---------------------------------------------------------------------------
# Interactive camera-based test tool for the full detection + sorting pipeline.
# No hardware (G-code) required — just a webcam.
#
# Usage:
#   python test_sorting_live.py
#
# Controls:
#   SPACE  — Detect and identify the card under the camera, show bin result
#   b      — Reconfigure the bounding box
#   m      — Change sorting mode (cycles through all modes including custom)
#   s      — Show current sort config status (bin counts)
#   r      — Reset bin counts (start fresh)
#   ESC    — Quit
# ---------------------------------------------------------------------------

import os
import sys
import glob
import cv2
import numpy as np
from PIL import Image

print("Loading files, please wait...")

from config import (
    CROP_SIZE, PHASH_DISTANCE_THRESHOLD, PHASH_CLOSE_MATCH_DIFF,
    EXCLUDED_SETS, SORTING_MODES, SORT_CONFIGS_DIR,
)
from detection import setup_bounding_box, load_bounding_box, crop_to_bounding_box, determine_orientation
from cards import extract_card_info, CARD_DATA_BY_ID
from hashing import compute_distances_for_image, compute_combined_distances
from sorting import get_bin_number, set_sort_config, get_sort_config
from sort_config import SortConfig, prompt_manual_config
from scan_tracker import ScanTracker


# ---------------------------------------------------------------------------
# Card identification (same as test_ocr.py)
# ---------------------------------------------------------------------------

def identify_card(card_img):
    """
    Identify a card using hash matching (primary) with OCR disambiguation.
    Returns (card_info, method_str, card_data) or (None, None, None).
    """
    cropped = card_img[0:CROP_SIZE, 0:CROP_SIZE]
    img_pil = Image.fromarray(cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB))
    all_dists = compute_combined_distances(img_pil, hash_size=16)

    allowed = []
    for cid, dist in all_dists:
        cdata = CARD_DATA_BY_ID.get(cid, {})
        if 'paper' not in cdata.get('games', []):
            continue
        if cdata.get('set', '').lower() in EXCLUDED_SETS:
            continue
        allowed.append((cid, dist))
    allowed.sort(key=lambda x: x[1])

    if not allowed:
        return None, None, None

    top_id, top_dist = allowed[0]

    print(f"[hash] Top 5 matches:")
    for rank, (cid, dist) in enumerate(allowed[:5], start=1):
        cname = CARD_DATA_BY_ID.get(cid, {}).get('name', '?')
        cset = CARD_DATA_BY_ID.get(cid, {}).get('set', '?')
        print(f"  #{rank}: {cname} ({cset}) dist={dist:.2f}")

    if top_dist > PHASH_DISTANCE_THRESHOLD:
        print(f"[hash] Distance {top_dist:.2f} > threshold {PHASH_DISTANCE_THRESHOLD}, unrecognized.")
        return None, None, None

    # Check ambiguity
    is_ambiguous = False
    if len(allowed) > 1:
        _, second_dist = allowed[1]
        diff = second_dist - top_dist
        if diff < PHASH_CLOSE_MATCH_DIFF:
            is_ambiguous = True
            print(f"[hash] Ambiguous: top two differ by only {diff:.2f}")

    if not is_ambiguous:
        info = extract_card_info(top_id)
        card_data = CARD_DATA_BY_ID.get(top_id)
        return info, "hash", card_data

    # Ambiguous match — return top match but flag it
    info = extract_card_info(top_id)
    card_data = CARD_DATA_BY_ID.get(top_id)
    return info, "hash_ambiguous", card_data


# ---------------------------------------------------------------------------
# Sort mode selection
# ---------------------------------------------------------------------------

def select_sort_mode():
    """
    Prompt the user to pick a sorting mode. Handles custom config loading.
    Returns the mode string (e.g., "color", "custom_file").
    """
    print("\n" + "=" * 50)
    print("SELECT SORTING MODE")
    print("=" * 50)
    for key in sorted(SORTING_MODES.keys()):
        print(f"  {key} - {SORTING_MODES[key]}")
    print()

    choice = input("Enter mode number (or press Enter for color): ").strip()
    mode = SORTING_MODES.get(choice, "color")
    print(f"Selected: {mode}")

    if mode == "custom_file":
        os.makedirs(SORT_CONFIGS_DIR, exist_ok=True)
        configs = sorted(glob.glob(os.path.join(SORT_CONFIGS_DIR, "*.txt")))
        if configs:
            print("\nAvailable configs:")
            for i, path in enumerate(configs, 1):
                print(f"  {i}. {os.path.basename(path)}")
            print()

        filepath = input("Config file path (or number): ").strip()
        try:
            idx = int(filepath) - 1
            if 0 <= idx < len(configs):
                filepath = configs[idx]
        except (ValueError, IndexError):
            pass

        if not os.path.isabs(filepath) and not os.path.exists(filepath):
            filepath = os.path.join(SORT_CONFIGS_DIR, filepath)

        try:
            sort_cfg = SortConfig.from_file(filepath)
            set_sort_config(sort_cfg)
            print(f"\n{sort_cfg.describe()}")
        except Exception as e:
            print(f"ERROR loading config: {e}")
            print("Falling back to color sorting.")
            mode = "color"

    elif mode == "custom_manual":
        try:
            sort_cfg = prompt_manual_config()
            set_sort_config(sort_cfg)
        except Exception as e:
            print(f"ERROR: {e}")
            print("Falling back to color sorting.")
            mode = "color"

    return mode


# ---------------------------------------------------------------------------
# Overlay drawing
# ---------------------------------------------------------------------------

def draw_result_overlay(frame, card_info, method, bin_number, mode, sort_cfg=None):
    """Draw detection results as an overlay on the frame."""
    y = 30

    if card_info:
        name = card_info.get("Name", "?")
        sets = card_info.get("Sets", [card_info.get("Set", "?")])
        colors = card_info.get("Colors", [])
        cmc = card_info.get("CMC", "?")
        types = card_info.get("Types", [])
        price = card_info.get("Price", "?")

        cv2.putText(frame, f"{name}", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        y += 30
        cv2.putText(frame, f"Method: {method}", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        y += 25
        cv2.putText(frame, f"Sets: {', '.join(sets)}", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        y += 25
        cv2.putText(frame, f"Colors: {', '.join(colors) if colors else 'Colorless'}  |  CMC: {cmc}", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        y += 25
        cv2.putText(frame, f"Types: {', '.join(types)}  |  Price: {price}", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        y += 35

        # Bin assignment — big and prominent
        cv2.putText(frame, f"BIN {bin_number}  ({mode})", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
        y += 35

        # Show sort config query for custom modes
        if sort_cfg and mode in ("custom_file", "custom_manual"):
            for bnum, qstr, _ast in sort_cfg.bin_queries:
                if bnum == bin_number:
                    cv2.putText(frame, f"Matched: {qstr}", (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 200), 1)
                    y += 25
                    break
            else:
                if bin_number == sort_cfg.fallback_bin:
                    cv2.putText(frame, "Matched: [fallback — no query matched]", (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 200), 1)
                    y += 25
    else:
        cv2.putText(frame, "UNRECOGNIZED", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        y += 35
        cv2.putText(frame, f"Would go to fallback bin ({mode})", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)

    return frame


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    # Select sort mode
    current_mode = select_sort_mode()

    # Open webcam
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Cannot open webcam.")
        return

    # Bounding box
    bounding_corners = load_bounding_box()
    if bounding_corners is not None:
        print("Using saved bounding box. Press 'b' to reconfigure.")
    else:
        print("No saved bounding box. Click 4 corners to set one.")
        bounding_corners = setup_bounding_box(cap)
        if bounding_corners is None:
            print("Cancelled.")
            cap.release()
            return

    # Start scan tracker
    tracker = ScanTracker()
    sort_cfg = get_sort_config()
    config_name = None
    bin_count = 10
    if sort_cfg:
        config_name = getattr(sort_cfg, '_config_name', None)
        bin_count = sort_cfg.bin_count
    tracker.start_session(
        sort_mode=current_mode,
        config_name=config_name,
        bin_count=bin_count,
    )

    print()
    print("=" * 50)
    print("LIVE SORTING TEST")
    print("=" * 50)
    print(f"  Mode:  {current_mode}")
    if sort_cfg:
        print(f"  Bins:  {sort_cfg.bin_count}, fallback=bin {sort_cfg.fallback_bin}")
        if sort_cfg.bin_limit:
            print(f"  Limit: {sort_cfg.bin_limit} cards/bin")
    print()
    print()
    print("Controls:")
    print("  SPACE  — Detect card and show bin assignment")
    print("  b      — Reconfigure bounding box")
    print("  m      — Change sorting mode")
    print("  s      — Show session status & bin counts")
    print("  c      — Show collection overview")
    print("  i      — Show inventory (all cards in DB)")
    print("  r      — Reset bin counts")
    print("  1-9    — Show contents of bin 1-9")
    print("  ESC    — Quit")
    print()

    cards_scanned = 0
    cv2.namedWindow("Live Test", cv2.WINDOW_AUTOSIZE)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

        # Draw bounding box and mode info on live view
        display = frame.copy()
        if bounding_corners is not None:
            pts = bounding_corners.astype(int)
            for i in range(4):
                cv2.line(display, tuple(pts[i]), tuple(pts[(i+1) % 4]),
                         (0, 255, 0), 2)

        # Mode label in top-right
        h, w = display.shape[:2]
        cv2.putText(display, f"Mode: {current_mode}", (w - 250, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
        cv2.putText(display, f"Cards: {cards_scanned}", (w - 250, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)

        cv2.imshow("Live Test", display)

        key = cv2.waitKey(1) & 0xFF

        if key == 27:  # ESC
            break

        elif key == ord('b'):
            bounding_corners = setup_bounding_box(cap)
            if bounding_corners is None:
                bounding_corners = load_bounding_box()

        elif key == ord('m'):
            # End current session and start a new one
            tracker.end_session()
            cv2.destroyAllWindows()
            current_mode = select_sort_mode()
            sort_cfg = get_sort_config()
            cards_scanned = 0
            tracker = ScanTracker()
            cfg_name = getattr(sort_cfg, '_config_name', None) if sort_cfg else None
            b_count = sort_cfg.bin_count if sort_cfg else 10
            tracker.start_session(sort_mode=current_mode, config_name=cfg_name,
                                  bin_count=b_count)
            cv2.namedWindow("Live Test", cv2.WINDOW_AUTOSIZE)
            print(f"\nMode changed to: {current_mode}")
            print("New tracking session started.\n")

        elif key == ord('s'):
            tracker.print_status()
            sort_cfg = get_sort_config()
            if sort_cfg:
                print(sort_cfg.get_status())

        elif key == ord('r'):
            sort_cfg = get_sort_config()
            if sort_cfg:
                sort_cfg.reset_counts()
            # End current session and start fresh
            tracker.end_session()
            cards_scanned = 0
            tracker = ScanTracker()
            cfg_name = getattr(sort_cfg, '_config_name', None) if sort_cfg else None
            b_count = sort_cfg.bin_count if sort_cfg else 10
            tracker.start_session(sort_mode=current_mode, config_name=cfg_name,
                                  bin_count=b_count)
            print("Bin counts reset. New tracking session started.")

        elif key == ord('c'):
            tracker.print_collection_stats()

        elif key == ord('i'):
            tracker.print_inventory(limit=30)

        elif key in range(ord('1'), ord('9') + 1):
            # Number keys 1-9: show bin contents
            bin_num = key - ord('0')
            tracker.print_bin(bin_num)

        elif key == 32:  # SPACE
            if bounding_corners is None:
                print("No bounding box. Press 'b' to set one.")
                continue

            print("\n" + "=" * 60)
            print(f"[scan #{cards_scanned + 1}] Detecting card...")

            # Crop and correct
            card_img = crop_to_bounding_box(frame, bounding_corners)
            cv2.imshow("Card (raw crop)", card_img)

            # Orientation
            card_img, was_rotated = determine_orientation(card_img)
            if was_rotated:
                print("  Card was upside-down, rotated 180.")
            cv2.imshow("Card (oriented)", card_img)

            # Identify
            card_info, method, card_data = identify_card(card_img)

            # Get hash distance for tracking
            hash_distance = None
            if method and 'hash' in method:
                # Re-extract distance from the identify flow
                cropped = card_img[0:CROP_SIZE, 0:CROP_SIZE]
                img_pil = Image.fromarray(cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB))
                all_dists = compute_combined_distances(img_pil, hash_size=16)
                if all_dists:
                    all_dists.sort(key=lambda x: x[1])
                    hash_distance = all_dists[0][1]

            # Determine bin
            sort_cfg = get_sort_config()
            if card_info:
                bin_number = get_bin_number(card_info, current_mode, card_data=card_data)
                sets = card_info.get('Sets', [card_info.get('Set', '?')])

                print(f"\n  RESULT: {card_info['Name']}")
                print(f"  Sets:   {', '.join(sets)}")
                print(f"  Method: {method}")
                print(f"  Colors: {card_info.get('Colors', [])}")
                print(f"  CMC:    {card_info.get('CMC', '?')}")
                print(f"  Types:  {card_info.get('Types', [])}")
                print(f"  Price:  {card_info.get('Price', '?')}")
                print(f"  >>> BIN {bin_number} ({current_mode})")

                # Show which query matched for custom modes
                if sort_cfg and current_mode in ("custom_file", "custom_manual"):
                    for bnum, qstr, _ast in sort_cfg.bin_queries:
                        if bnum == bin_number:
                            print(f"  >>> Matched query: {qstr}")
                            break
                    else:
                        if bin_number == sort_cfg.fallback_bin:
                            print(f"  >>> Fallback bin (no query matched)")

                tracker.record_scan(card_info=card_info, bin_num=bin_number,
                                    method=method, hash_distance=hash_distance,
                                    card_data=card_data)
            else:
                bin_number = 10
                print(f"\n  RESULT: Unrecognized")
                print(f"  >>> BIN {bin_number} (error/unrecognized)")
                tracker.record_scan(card_info=None, bin_num=bin_number,
                                    hash_distance=hash_distance)

            cards_scanned += 1
            print("=" * 60)

            # Show result overlay
            result_frame = frame.copy()
            result_frame = draw_result_overlay(result_frame, card_info, method,
                                               bin_number, current_mode, sort_cfg)
            cv2.imshow("Last Result", result_frame)

    cap.release()
    cv2.destroyAllWindows()

    # Final summary
    tracker.print_status()
    tracker.print_collection_stats()
    tracker.end_session()


if __name__ == "__main__":
    main()
