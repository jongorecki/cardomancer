# test_full_sort.py
# ---------------------------------------------------------------------------
# Full end-to-end sorting test: camera + detection + G-code motion.
#
# Usage:
#   python test_full_sort.py
#
# Controls:
#   SPACE  — Run one full sort cycle (detect -> pick -> drop)
#   a      — Start auto-sort (continuous pipelined sorting)
#   d      — Detect only (no pick/sort) — for testing detection
#   b      — Reconfigure the bounding box
#   m      — Change sorting mode
#   s      — Show session status & bin counts
#   h      — Home all axes
#   ESC    — Quit (homes Z, turns off pumps)
# ---------------------------------------------------------------------------

import os
import sys
import glob
import time
import threading
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
print("Loading files, please wait...")

from config import (
    PHASH_DISTANCE_THRESHOLD, PHASH_CLOSE_MATCH_DIFF,
    EXCLUDED_SETS, SORTING_MODES, SORT_CONFIGS_DIR,
)
from detection import setup_bounding_box, load_bounding_box, crop_to_bounding_box, determine_orientation
from cards import extract_card_info, CARD_DATA_BY_ID
from hashing import compute_combined_distances_with_rerank
from sorting import get_bin_number, set_sort_config, get_sort_config
from sort_config import SortConfig, prompt_manual_config
from scan_tracker import ScanTracker
from gcode_control import (
    connect_to_board,
    close_connection,
    is_connected,
    home_all,
    move_to_detection_position,
    send_to_bin,
    pick_and_drop,
    return_to_detection_nonblocking,
    wait_for_completion,
    configure_bins,
    all_pumps_off,
    z_to_top,
)


# ---------------------------------------------------------------------------
# Card identification (same as test_sorting_live.py)
# ---------------------------------------------------------------------------

def identify_card(card_img):
    """
    Identify a card using two-stage matching:
      Stage 1: Multi-hash (phash + dhash + whash) on art region
      Stage 2: Histogram correlation re-ranking on top 30 candidates

    Returns (card_info, method_str, card_data, hash_distance)
    or (None, None, None, None).
    """
    images_dir = os.path.join(os.path.dirname(__file__), "downloaded_cards")
    all_dists = compute_combined_distances_with_rerank(
        card_img, hash_size=16, rerank_top=30, images_dir=images_dir
    )

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
        return None, None, None, None

    top_id, top_dist = allowed[0]

    print(f"[match] Top 5 matches:")
    for rank, (cid, dist) in enumerate(allowed[:5], start=1):
        cname = CARD_DATA_BY_ID.get(cid, {}).get('name', '?')
        cset = CARD_DATA_BY_ID.get(cid, {}).get('set', '?')
        print(f"  #{rank}: {cname} ({cset}) dist={dist:.2f}")

    if top_dist > PHASH_DISTANCE_THRESHOLD:
        print(f"[match] Distance {top_dist:.2f} > threshold {PHASH_DISTANCE_THRESHOLD}, unrecognized.")
        return None, None, None, top_dist

    info = extract_card_info(top_id)
    card_data = CARD_DATA_BY_ID.get(top_id)

    # Note if match was ambiguous
    if len(allowed) > 1:
        _, second_dist = allowed[1]
        diff = second_dist - top_dist
        if diff < PHASH_CLOSE_MATCH_DIFF:
            print(f"[match] Ambiguous: top two differ by only {diff:.2f} (using best match)")
            return info, "hash_ambiguous", card_data, top_dist

    return info, "hash", card_data, top_dist


def _identify_pipeline(card_img):
    """
    Full detection pipeline: orientation check + identification.
    Safe to run in a background thread (no serial/GUI access).
    Returns (card_info, method, card_data, hash_distance).
    """
    card_img, was_rotated = determine_orientation(card_img)
    if was_rotated:
        print("  (Card was upside-down, rotated 180)")
    return identify_card(card_img)


# ---------------------------------------------------------------------------
# Motion worker thread
# ---------------------------------------------------------------------------

_motion_stop = threading.Event()
_motion_busy = threading.Event()    # Set while motion is in progress


def _motion_worker(command_queue):
    """
    Dedicated thread for serial communication.
    Receives (action, args) tuples from the queue.
    Actions: 'pick_drop', 'send_to_bin', 'return', 'stop'
    """
    while not _motion_stop.is_set():
        try:
            cmd = command_queue.get(timeout=0.1)
        except Exception:
            continue

        action = cmd[0]
        if action == 'stop':
            break
        elif action == 'pick_drop':
            bin_number = cmd[1]
            _motion_busy.set()
            pick_and_drop(bin_number)
            return_to_detection_nonblocking()
            wait_for_completion()
            _motion_busy.clear()
        elif action == 'send_to_bin':
            bin_number = cmd[1]
            _motion_busy.set()
            send_to_bin(bin_number)
            _motion_busy.clear()


# ---------------------------------------------------------------------------
# Auto-sort (pipelined continuous sorting)
# ---------------------------------------------------------------------------

def _capture_and_crop(cap, bounding_corners):
    """Grab frames to let auto-expose settle, crop to bounding box."""
    for _ in range(5):
        ret, frame = cap.read()
    if not ret:
        return None, None
    frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    card_img = crop_to_bounding_box(frame, bounding_corners)
    return frame, card_img


def run_auto_sort(cap, bounding_corners, current_mode, tracker, cards_scanned):
    """
    Continuous pipelined sorting loop.

    Pipeline:
      1. Detect card N (already done in background, or sync for first card)
      2. Motion: pick card N, drop in bin, return to detection position
      3. While motion runs: capture + detect card N+1 in background thread
      4. When motion finishes, go to step 1 with N+1's result ready

    Returns updated cards_scanned count.
    Press ESC to stop and return to manual mode.
    """
    import queue

    print("\n" + "=" * 60)
    print("AUTO-SORT MODE — Press ESC to stop")
    print("=" * 60)

    # Start the motion worker thread
    motion_queue = queue.Queue()
    _motion_stop.clear()
    _motion_busy.clear()
    motion_thread = threading.Thread(target=_motion_worker, args=(motion_queue,),
                                     daemon=True)
    motion_thread.start()

    # Thread pool for background detection (1 worker — CPU-bound hashing)
    executor = ThreadPoolExecutor(max_workers=1)
    pending_future = None     # Future for card N+1 detection
    stop_requested = False
    cycle_times = []

    try:
        while not stop_requested:
            cycle_start = time.time()
            cards_scanned += 1

            # ----------------------------------------------------------
            # STEP 1: Get detection result for this card
            # ----------------------------------------------------------
            if pending_future is not None:
                # We already submitted detection in the background — wait for it
                print(f"\n[auto #{cards_scanned}] Waiting for background detection...")
                while not pending_future.done():
                    # Pump OpenCV event loop so UI stays responsive
                    key = cv2.waitKey(10) & 0xFF
                    if key == 27:
                        stop_requested = True
                        break
                if stop_requested:
                    break

                card_info, method, card_data, hash_distance = pending_future.result()
                pending_future = None
            else:
                # First card — detect synchronously
                print(f"\n[auto #{cards_scanned}] Capturing first card...")
                frame, card_img = _capture_and_crop(cap, bounding_corners)
                if card_img is None:
                    print("  [auto] Camera read failed, stopping.")
                    break
                cv2.imshow("Card (crop)", card_img)
                cv2.waitKey(1)

                card_info, method, card_data, hash_distance = _identify_pipeline(card_img)

            # ----------------------------------------------------------
            # STEP 2: Determine bin
            # ----------------------------------------------------------
            sort_cfg = get_sort_config()
            if card_info:
                bin_number = get_bin_number(card_info, current_mode, card_data=card_data)
                print(f"  DETECTED: {card_info['Name']}  →  Bin {bin_number}")
                print(f"  Method: {method}, Distance: {hash_distance:.2f}")
            else:
                bin_number = sort_cfg.fallback_bin if sort_cfg else 10
                print(f"  UNRECOGNIZED — fallback bin {bin_number}")

            # ----------------------------------------------------------
            # STEP 3: Start motion (pick, drop, return to detection)
            # ----------------------------------------------------------
            print(f"  [auto] Motion: picking card → bin {bin_number}...")
            _motion_busy.set()
            motion_queue.put(('pick_drop', bin_number))

            # ----------------------------------------------------------
            # STEP 4: While motion is running, wait for it to finish
            #         and return to detection, then capture + detect N+1
            # ----------------------------------------------------------
            # Wait for motion to complete (carriage back at detection position)
            while _motion_busy.is_set():
                key = cv2.waitKey(10) & 0xFF
                if key == 27:
                    stop_requested = True
                    break

                # Update live view while waiting
                ret, live_frame = cap.read()
                if ret:
                    live_frame = cv2.rotate(live_frame, cv2.ROTATE_90_CLOCKWISE)
                    disp = live_frame.copy()
                    if bounding_corners is not None:
                        pts = bounding_corners.astype(int)
                        for i in range(4):
                            cv2.line(disp, tuple(pts[i]), tuple(pts[(i + 1) % 4]),
                                     (0, 255, 0), 2)
                    h_d, w_d = disp.shape[:2]
                    cv2.putText(disp, "AUTO-SORT (ESC to stop)", (10, 25),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                    cv2.putText(disp, f"Cards: {cards_scanned}", (w_d - 200, 25),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
                    cv2.imshow("Live View", disp)

            if stop_requested:
                break

            # ----------------------------------------------------------
            # STEP 5: Motion done — capture next card and start detection
            #         in background thread
            # ----------------------------------------------------------
            frame, card_img = _capture_and_crop(cap, bounding_corners)
            if card_img is not None:
                cv2.imshow("Card (crop)", card_img)
                cv2.waitKey(1)
                # Submit background detection for next card
                pending_future = executor.submit(_identify_pipeline, card_img.copy())
            else:
                print("  [auto] Camera read failed, stopping.")
                break

            # Record this card's result
            tracker.record_scan(
                card_info=card_info,
                bin_num=bin_number,
                method=method,
                hash_distance=hash_distance,
                card_data=card_data,
            )

            cycle_time = time.time() - cycle_start
            cycle_times.append(cycle_time)
            avg_time = sum(cycle_times) / len(cycle_times)
            cards_per_hour = 3600.0 / avg_time if avg_time > 0 else 0
            print(f"  [auto] Cycle: {cycle_time:.2f}s  "
                  f"Avg: {avg_time:.2f}s  "
                  f"Rate: {cards_per_hour:.0f} cards/hr")

    except KeyboardInterrupt:
        print("\n[auto] Interrupted.")

    finally:
        # Shut down motion worker
        motion_queue.put(('stop',))
        motion_thread.join(timeout=5)
        executor.shutdown(wait=False)
        _motion_stop.set()

    # Print summary
    if cycle_times:
        avg = sum(cycle_times) / len(cycle_times)
        rate = 3600.0 / avg if avg > 0 else 0
        print(f"\n[auto] Session: {len(cycle_times)} cards, "
              f"avg {avg:.2f}s/card, ~{rate:.0f} cards/hr")
        print(f"  Fastest: {min(cycle_times):.2f}s  "
              f"Slowest: {max(cycle_times):.2f}s")

    print("[auto] Returning to manual mode.\n")
    return cards_scanned


# ---------------------------------------------------------------------------
# Sort mode selection
# ---------------------------------------------------------------------------

def select_sort_mode():
    """Prompt the user to pick a sorting mode."""
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
# Main
# ---------------------------------------------------------------------------

def main():
    # --- 1) Connect to board ---
    print("\n[setup] Connecting to control board...")
    connect_to_board()
    if not is_connected():
        print("[setup] ERROR: Could not connect. Exiting.")
        return

    # --- 2) Home all axes ---
    print("[setup] Homing all axes (Z first, then X)...")
    home_all()

    # --- 3) Move to detection position ---
    print("[setup] Moving to detection position...")
    move_to_detection_position()

    # --- 4) Open webcam ---
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Cannot open webcam.")
        close_connection()
        return

    # --- 5) Bounding box ---
    bounding_corners = load_bounding_box()
    if bounding_corners is not None:
        print("Using saved bounding box. Press 'b' to reconfigure.")
    else:
        print("No saved bounding box. Click 4 corners to set one.")
        bounding_corners = setup_bounding_box(cap)
        if bounding_corners is None:
            print("Cancelled.")
            cap.release()
            close_connection()
            return

    # --- 6) Select sort mode ---
    current_mode = select_sort_mode()

    # --- 7) Configure bins for G-code ---
    sort_cfg = get_sort_config()
    bin_count = sort_cfg.bin_count if sort_cfg else 10
    configure_bins(bin_count)

    # --- 8) Start tracker ---
    tracker = ScanTracker()
    config_name = getattr(sort_cfg, '_config_name', None) if sort_cfg else None
    tracker.start_session(
        sort_mode=current_mode,
        config_name=config_name,
        bin_count=bin_count,
    )

    print()
    print("=" * 50)
    print("FULL SORT TEST")
    print("=" * 50)
    print(f"  Mode:  {current_mode}")
    if sort_cfg:
        print(f"  Bins:  {sort_cfg.bin_count}, fallback=bin {sort_cfg.fallback_bin}")
        if sort_cfg.bin_limit:
            print(f"  Limit: {sort_cfg.bin_limit} cards/bin")
    print()
    print("Controls:")
    print("  SPACE  — Sort one card (detect → pick → drop)")
    print("  a      — Auto-sort (continuous pipelined sorting)")
    print("  d      — Detect only (no motion) — test detection")
    print("  b      — Reconfigure bounding box")
    print("  m      — Change sorting mode")
    print("  s      — Show session status & bin counts")
    print("  h      — Home all axes")
    print("  ESC    — Quit (safe shutdown)")
    print()

    cards_scanned = 0
    debug_mode = False
    cv2.namedWindow("Live View", cv2.WINDOW_AUTOSIZE)

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

            # Draw bounding box on live view
            display = frame.copy()
            if bounding_corners is not None:
                pts = bounding_corners.astype(int)
                for i in range(4):
                    cv2.line(display, tuple(pts[i]), tuple(pts[(i + 1) % 4]),
                             (0, 255, 0), 2)

            h, w = display.shape[:2]
            cv2.putText(display, f"Mode: {current_mode}", (w - 250, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
            cv2.putText(display, f"Cards: {cards_scanned}", (w - 250, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
            cv2.putText(display, "SPACE=sort | a=auto | d=detect | ESC=quit", (10, h - 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)

            cv2.imshow("Live View", display)

            key = cv2.waitKey(1) & 0xFF

            if key == 27:  # ESC
                break

            elif key == ord('b'):
                bounding_corners = setup_bounding_box(cap)
                if bounding_corners is None:
                    bounding_corners = load_bounding_box()

            elif key == ord('m'):
                tracker.end_session()
                cv2.destroyAllWindows()
                current_mode = select_sort_mode()
                sort_cfg = get_sort_config()
                cards_scanned = 0
                bin_count = sort_cfg.bin_count if sort_cfg else 10
                configure_bins(bin_count)
                tracker = ScanTracker()
                cfg_name = getattr(sort_cfg, '_config_name', None) if sort_cfg else None
                tracker.start_session(sort_mode=current_mode, config_name=cfg_name,
                                      bin_count=bin_count)
                cv2.namedWindow("Live View", cv2.WINDOW_AUTOSIZE)
                print(f"\nMode changed to: {current_mode}\n")

            elif key == ord('s'):
                tracker.print_status()
                sort_cfg = get_sort_config()
                if sort_cfg:
                    print(sort_cfg.get_status())

            elif key == ord('h'):
                print("[manual] Homing all axes...")
                home_all()
                move_to_detection_position()
                print("[manual] Done.")

            elif key == ord('a'):  # Auto-sort mode
                if bounding_corners is None:
                    print("No bounding box. Press 'b' to set one.")
                    continue
                cards_scanned = run_auto_sort(
                    cap, bounding_corners, current_mode, tracker, cards_scanned
                )

            elif key == ord('d'):  # Detect only — no motion
                if bounding_corners is None:
                    print("No bounding box. Press 'b' to set one.")
                    continue

                print("\n" + "-" * 60)
                print("[detect-only] Testing detection...")
                print("-" * 60)

                for _ in range(5):
                    ret, frame = cap.read()
                if not ret:
                    continue
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

                card_img = crop_to_bounding_box(frame, bounding_corners, debug=True)
                cv2.imshow("Card (crop)", card_img)

                card_img, was_rotated = determine_orientation(card_img)
                if was_rotated:
                    print("  Card was upside-down, rotated 180.")
                cv2.imshow("Card (oriented)", card_img)

                card_info, method, card_data, hash_dist = identify_card(card_img)

                if card_info:
                    print(f"\n  DETECTED: {card_info['Name']}")
                    print(f"  Method:   {method}")
                    print(f"  Distance: {hash_dist:.2f}")
                    print(f"  Colors:   {card_info.get('Colors', [])}")
                    print(f"  Types:    {card_info.get('Types', [])}")
                    sort_cfg = get_sort_config()
                    bin_number = get_bin_number(card_info, current_mode, card_data=card_data)
                    print(f"  Would go to: Bin {bin_number}")
                else:
                    print(f"\n  UNRECOGNIZED (distance: {hash_dist})")
                    debug_dir = os.path.join(os.path.dirname(__file__), "debug_captures")
                    os.makedirs(debug_dir, exist_ok=True)
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    cv2.imwrite(os.path.join(debug_dir, f"fail_{ts}_frame.png"), frame)
                    cv2.imwrite(os.path.join(debug_dir, f"fail_{ts}_card.png"), card_img)
                    print(f"  [debug] Saved to debug_captures/")

                print("-" * 60)
                print()

            elif key == 32:  # SPACE — full sort cycle
                if bounding_corners is None:
                    print("No bounding box. Press 'b' to set one.")
                    continue

                print("\n" + "=" * 60)
                print(f"[sort #{cards_scanned + 1}] STEP 1: Detecting card...")
                print("=" * 60)

                # --- DETECT ---
                # Grab multiple frames to let camera auto-expose settle
                for _ in range(5):
                    ret, frame = cap.read()
                if not ret:
                    continue
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

                card_img = crop_to_bounding_box(frame, bounding_corners)
                cv2.imshow("Card (crop)", card_img)

                card_img, was_rotated = determine_orientation(card_img)
                if was_rotated:
                    print("  Card was upside-down, rotated 180.")
                cv2.imshow("Card (oriented)", card_img)

                # --- IDENTIFY ---
                card_info, method, card_data, hash_distance = identify_card(card_img)

                # Save debug images on failure
                if card_info is None:
                    debug_dir = os.path.join(os.path.dirname(__file__), "debug_captures")
                    os.makedirs(debug_dir, exist_ok=True)
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    cv2.imwrite(os.path.join(debug_dir, f"fail_{ts}_frame.png"), frame)
                    cv2.imwrite(os.path.join(debug_dir, f"fail_{ts}_card.png"), card_img)
                    print(f"  [debug] Saved failed detection images to debug_captures/")

                # --- DETERMINE BIN ---
                sort_cfg = get_sort_config()
                if card_info:
                    bin_number = get_bin_number(card_info, current_mode, card_data=card_data)
                    print(f"\n  DETECTED: {card_info['Name']}")
                    print(f"  Method:   {method}")
                    print(f"  Colors:   {card_info.get('Colors', [])}")
                    print(f"  Types:    {card_info.get('Types', [])}")
                    print(f"  >>> SORTING TO BIN {bin_number}")
                else:
                    sort_cfg = get_sort_config()
                    bin_number = sort_cfg.fallback_bin if sort_cfg else 10
                    print(f"\n  UNRECOGNIZED — sending to fallback bin {bin_number}")

                # --- PICK AND SORT ---
                print(f"\n  STEP 2: Picking card and moving to bin {bin_number}...")
                send_to_bin(bin_number)

                # --- RECORD ---
                tracker.record_scan(
                    card_info=card_info,
                    bin_num=bin_number,
                    method=method,
                    hash_distance=hash_distance,
                    card_data=card_data,
                )

                cards_scanned += 1
                print(f"\n  DONE. {cards_scanned} cards sorted so far.")
                print("=" * 60)
                print()

    except KeyboardInterrupt:
        print("\n[interrupted] Shutting down...")

    finally:
        # Safe shutdown
        print("\n[shutdown] Turning off pumps...")
        all_pumps_off()
        print("[shutdown] Moving Z to top...")
        z_to_top()
        cap.release()
        cv2.destroyAllWindows()
        tracker.print_status()
        tracker.end_session()
        close_connection()
        print("[shutdown] Done.")


if __name__ == "__main__":
    main()
