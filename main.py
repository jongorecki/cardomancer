# main.py
# ---------------------------------------------------------------------------
# Main entry point for the MTG Card Sorter.
# Uses click-to-set bounding box + perceptual hash matching for card identification.
# ---------------------------------------------------------------------------

import os
import sys
import time
import cv2
import numpy as np
from PIL import Image

print("Loading files, please wait...")

# --- Config and Modules ---
from config import (
    CROP_SIZE,
    PHASH_DISTANCE_THRESHOLD,
    PHASH_CLOSE_MATCH_DIFF,
    SORTING_MODES,
    EXCLUDED_SETS,
    CARD_WIDTH,
    CARD_HEIGHT,
)

from detection import determine_orientation

# Hash-based fallback
from cards import (
    extract_card_info,
    card_is_allowed,
    get_illustration_id,
    CARD_DATA_BY_ID,
)
from hashing import hash_image_color, compute_distances_for_image, compute_combined_distances

# Sorting
from sorting import print_sorting_options, draw_info_as_json, set_sort_config, get_sort_config

# Detection (bounding box)
from detection import (
    setup_bounding_box,
    load_bounding_box,
    crop_to_bounding_box,
    get_perspective_corrected_card,
)

# Sort config (custom query-based sorting)
from sort_config import SortConfig, prompt_manual_config
from config import SORT_CONFIGS_DIR

# G-code control
from gcode_control import (
    connect_to_board,
    close_connection,
    is_connected,
    home_all,
    move_to_detection_position,
    send_to_bin,
    configure_bins,
    wait_for_completion,
    all_pumps_off,
)

# Scan tracking
from scan_tracker import ScanTracker


def identify_card_hash(card_img, layout="normal"):
    """
    Identify card using perceptual hash matching.
    Layout-aware: uses correct art region crop for sagas, classes, etc.
    Returns (card_info_dict, method_str, card_data) or (None, None, None).
    """
    all_distances = compute_combined_distances(card_img, hash_size=16, layout=layout)

    # Filter to allowed cards
    allowed = []
    for cid, dist in all_distances:
        cdata = CARD_DATA_BY_ID.get(cid, {})
        if 'paper' not in cdata.get('games', []):
            continue
        set_code = cdata.get('set', '').lower()
        if set_code not in EXCLUDED_SETS:
            allowed.append((cid, dist))

    allowed.sort(key=lambda x: x[1])

    if not allowed:
        return None, None

    top_id, top_dist = allowed[0]

    print(f"[Hash] Best match: {top_id}, distance: {top_dist:.2f}")

    if top_dist > PHASH_DISTANCE_THRESHOLD:
        print("[Hash] Distance too high, unrecognized.")
        return None, None, None

    # Check for ambiguous match
    if len(allowed) > 1:
        _, second_dist = allowed[1]
        diff = second_dist - top_dist
        if diff < PHASH_CLOSE_MATCH_DIFF:
            print(f"[Hash] Ambiguous match (diff={diff:.2f}), unrecognized.")
            return None, None, None

    info = extract_card_info(top_id)
    card_data = CARD_DATA_BY_ID.get(top_id)
    return info, "hash", card_data


def main():
    # 1) Connect to board
    connect_to_board()
    if not is_connected():
        print("[main] ERROR: Could not connect to board. Exiting.")
        sys.exit(1)

    # 2) Home all axes (Z first to clear bins, then X)
    print("[main] Homing all axes...")
    home_all()

    # 3) Move to detection position (Z top, X to camera offset)
    print("[main] Moving to detection position...")
    move_to_detection_position()

    # Prompt user to load cards
    print("[main] Please load the cards into the sorting bin.")
    input("[main] Press ENTER when ready...")

    # Prompt for sorting mode
    print_sorting_options()
    choice = input("Enter the number of the sorting method: ").strip()
    current_sorting_mode = SORTING_MODES.get(choice, "color")
    print(f"[main] Selected mode: {current_sorting_mode}")

    # All sort modes now resolve to a SortConfig. Built-in modes
    # (color/mana_value/set/price/type) load the corresponding
    # sort_configs/<mode>.txt file.
    if current_sorting_mode == "custom_file":
        import glob
        # List available config files
        os.makedirs(SORT_CONFIGS_DIR, exist_ok=True)
        configs = sorted(glob.glob(os.path.join(SORT_CONFIGS_DIR, "*.txt")))
        if configs:
            print("\nAvailable sort configs:")
            for i, path in enumerate(configs, 1):
                print(f"  {i}. {os.path.basename(path)}")
            print()

        filepath = input("Enter config file path (or number from list): ").strip()

        # Check if user entered a number from the list
        try:
            idx = int(filepath) - 1
            if 0 <= idx < len(configs):
                filepath = configs[idx]
        except (ValueError, IndexError):
            pass

        # If not an absolute path, look in sort_configs dir
        if not os.path.isabs(filepath) and not os.path.exists(filepath):
            filepath = os.path.join(SORT_CONFIGS_DIR, filepath)

        try:
            sort_cfg = SortConfig.from_file(filepath)
            set_sort_config(sort_cfg)
            if sort_cfg.bin_count != 10:
                configure_bins(sort_cfg.bin_count)
        except Exception as e:
            print(f"[main] ERROR loading sort config: {e}")
            print("[main] Falling back to color sorting.")
            current_sorting_mode = "color"

    elif current_sorting_mode == "custom_manual":
        try:
            sort_cfg = prompt_manual_config()
            set_sort_config(sort_cfg)
            if sort_cfg.bin_count != 10:
                configure_bins(sort_cfg.bin_count)
        except Exception as e:
            print(f"[main] ERROR setting up custom sort: {e}")
            print("[main] Falling back to color sorting.")
            current_sorting_mode = "color"

    # Built-in modes: load the shipped sort_configs/<mode>.txt
    if current_sorting_mode in ("color", "mana_value", "set", "price", "type"):
        builtin_path = os.path.join(SORT_CONFIGS_DIR,
                                    f"{current_sorting_mode}.txt")
        if os.path.exists(builtin_path):
            try:
                sort_cfg = SortConfig.from_file(builtin_path)
                set_sort_config(sort_cfg)
                if sort_cfg.bin_count != 10:
                    configure_bins(sort_cfg.bin_count)
            except Exception as e:
                print(f"[main] ERROR loading built-in sort config "
                      f"'{builtin_path}': {e}")
                raise SystemExit(1)
        else:
            print(f"[main] ERROR: built-in sort config not found at "
                  f"{builtin_path}")
            raise SystemExit(1)

    # Start scan tracker
    tracker = ScanTracker()
    config_name = None
    bin_count = 10
    if current_sorting_mode in ("custom_file", "custom_manual"):
        sort_cfg_active = None
        try:
            from sorting import get_sort_config as _get_sc
            sort_cfg_active = _get_sc()
        except Exception:
            pass
        if sort_cfg_active:
            config_name = getattr(sort_cfg_active, '_config_name', None)
            bin_count = sort_cfg_active.bin_count
    tracker.start_session(
        sort_mode=current_sorting_mode,
        config_name=config_name,
        bin_count=bin_count,
    )

    # Open webcam
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[main] Cannot open webcam.")
        close_connection()
        sys.exit(1)

    # Set up bounding box — try loading saved, otherwise click to set
    bounding_corners = load_bounding_box()
    if bounding_corners is not None:
        print("[main] Using saved bounding box. Press 'b' during detection to reconfigure.")
    else:
        print("[main] No saved bounding box found. Please define the card region.")
        bounding_corners = setup_bounding_box(cap)
        if bounding_corners is None:
            print("[main] Bounding box setup cancelled. Exiting.")
            cap.release()
            close_connection()
            sys.exit(1)

    print("[main] Press SPACE to detect card. Press 'b' to reset bounding box. Press ESC to exit.")

    cv2.namedWindow("Webcam (Live View)", cv2.WINDOW_AUTOSIZE)

    while True:
        ret, frame = cap.read()
        if not ret:
            print("[main] Failed to grab frame from camera.")
            break

        # Rotate webcam feed (same as before)
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

        # Draw bounding box on live view
        display = frame.copy()
        if bounding_corners is not None:
            pts = bounding_corners.astype(int)
            for i in range(4):
                cv2.line(display,
                         tuple(pts[i]), tuple(pts[(i+1) % 4]),
                         (0, 255, 0), 2)

        cv2.imshow("Webcam (Live View)", display)

        key = cv2.waitKey(1) & 0xFF
        if key == 27:  # ESC
            break
        elif key == ord('b'):
            # Reconfigure bounding box
            bounding_corners = setup_bounding_box(cap)
            if bounding_corners is None:
                print("[main] Bounding box reset cancelled, keeping previous.")
                bounding_corners = load_bounding_box()
        elif key == 32:  # SPACE => detect card
            if bounding_corners is None:
                print("[main] No bounding box defined. Press 'b' to set one.")
                continue

            print("\n[main] --- Detecting card ---")
            move_to_detection_position()
            time.sleep(0.5)

            # Grab a fresh frame after settling
            ret, frame = cap.read()
            if not ret:
                continue
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

            # Crop and perspective-correct to canonical card size
            card_img = crop_to_bounding_box(frame, bounding_corners)
            cv2.imshow("Card (raw crop)", card_img)

            # Determine orientation and layout
            card_img, was_rotated, card_layout = determine_orientation(card_img)
            if was_rotated:
                print("[main] Card was upside-down, rotated 180.")
            if card_layout != "normal":
                print(f"[main] Card layout: {card_layout}")
            cv2.imshow("Card (oriented)", card_img)

            # Identify card via hash matching
            card_info, method, card_data = identify_card_hash(card_img, layout=card_layout)

            # Display result and sort
            result_frame = frame.copy()
            if card_info:
                sets = card_info.get('Sets', [card_info['Set']])
                print(f"[main] Identified: {card_info['Name']} "
                      f"(sets={', '.join(sets)}, method={method})")
                draw_info_as_json(result_frame, card_info, 10, 30, 20)
                # Unified routing through SortConfig — no more mode dispatch.
                sort_cfg = get_sort_config()
                if sort_cfg is None:
                    print("[main] ERROR: No SortConfig active — cannot route card.")
                    bin_number = 10
                else:
                    bin_number = sort_cfg.get_bin(card_data)
                cv2.putText(result_frame, f"Bin: {bin_number}", (10, 200),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                tracker.record_scan(card_info=card_info, bin_num=bin_number,
                                    method=method, card_data=card_data)
                send_to_bin(bin_number, current_sorting_mode)
            else:
                print("[main] Card unrecognized.")
                cv2.putText(result_frame, "Unrecognized Card", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                tracker.record_scan(card_info=None, bin_num=10)
                send_to_bin(10, current_sorting_mode)

            cv2.imshow("Detected Card", result_frame)

    # Cleanup
    tracker.print_status()
    tracker.end_session()
    cap.release()
    cv2.destroyAllWindows()
    close_connection()


if __name__ == "__main__":
    main()
