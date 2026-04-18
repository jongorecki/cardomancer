# test_10_cards.py
# ---------------------------------------------------------------------------
# Quick test: detect and physically sort 10 cards, pause for accuracy check,
# log results, then optionally repeat.
#
# Uses pipelined detection (card N+1 identified while card N is in motion).
# ---------------------------------------------------------------------------

import os
import sys
import glob
import time
import threading
import queue
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor

print("Loading files, please wait...")

from config import (
    PHASH_DISTANCE_THRESHOLD, PHASH_CLOSE_MATCH_DIFF,
    EXCLUDED_SETS, SORTING_MODES, SORT_CONFIGS_DIR,
)
from ocr import determine_orientation
from detection import setup_bounding_box, load_bounding_box, crop_to_bounding_box
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

NUM_CARDS = 10
LOG_DIR = os.path.join(os.path.dirname(__file__), "test_logs")


# ---------------------------------------------------------------------------
# Card identification
# ---------------------------------------------------------------------------

def identify_card(card_img):
    """
    Identify a card using hash matching.
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

    print(f"  Top 3: ", end="")
    for rank, (cid, dist) in enumerate(allowed[:3], start=1):
        cname = CARD_DATA_BY_ID.get(cid, {}).get('name', '?')
        print(f"#{rank} {cname} ({dist:.1f})", end="  ")
    print()

    if top_dist > PHASH_DISTANCE_THRESHOLD:
        return None, None, None, top_dist

    info = extract_card_info(top_id)
    card_data = CARD_DATA_BY_ID.get(top_id)

    if len(allowed) > 1:
        _, second_dist = allowed[1]
        diff = second_dist - top_dist
        if diff < PHASH_CLOSE_MATCH_DIFF:
            return info, "hash_ambiguous", card_data, top_dist

    return info, "hash", card_data, top_dist


def _identify_pipeline(card_img):
    """Orientation check + identification. Safe for background thread."""
    card_img, was_rotated = determine_orientation(card_img)
    if was_rotated:
        print("  (rotated 180°)")
    return identify_card(card_img)


# ---------------------------------------------------------------------------
# Motion worker thread
# ---------------------------------------------------------------------------

_motion_stop = threading.Event()
_motion_busy = threading.Event()


def _motion_worker(command_queue):
    """Dedicated thread for serial G-code commands."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_and_crop(cap, bounding_corners):
    """Grab frames to settle auto-expose, then crop to bounding box."""
    for _ in range(5):
        ret, frame = cap.read()
    if not ret:
        return None, None
    frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    card_img = crop_to_bounding_box(frame, bounding_corners)
    return frame, card_img


def _update_live_view(cap, bounding_corners, overlay_text=None):
    """Read one frame, draw bounding box + optional overlay, show it."""
    ret, frame = cap.read()
    if not ret:
        return
    frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    disp = frame.copy()
    if bounding_corners is not None:
        pts = bounding_corners.astype(int)
        for i in range(4):
            cv2.line(disp, tuple(pts[i]), tuple(pts[(i + 1) % 4]),
                     (0, 255, 0), 2)
    if overlay_text:
        cv2.putText(disp, overlay_text, (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    cv2.imshow("Live View", disp)


# ---------------------------------------------------------------------------
# Batch sort loop
# ---------------------------------------------------------------------------

def _run_batch(cap, bounding_corners, current_mode, tracker):
    """
    Sort NUM_CARDS cards using pipelined detection + motion.
    Returns (results, bounding_corners) where results is a list of
    (card_num, name, set_code, bin, distance, cycle_time) tuples.
    bounding_corners may be updated if the user reconfigures after card 1.
    """
    motion_queue = queue.Queue()
    _motion_stop.clear()
    _motion_busy.clear()
    motion_thread = threading.Thread(target=_motion_worker, args=(motion_queue,),
                                     daemon=True)
    motion_thread.start()

    executor = ThreadPoolExecutor(max_workers=1)
    pending_future = None
    results = []
    aborted = False

    try:
        for card_num in range(1, NUM_CARDS + 1):
            cycle_start = time.time()

            # --- Get detection result ---
            if pending_future is not None:
                while not pending_future.done():
                    key = cv2.waitKey(10) & 0xFF
                    if key == 27:
                        aborted = True
                        break
                if aborted:
                    break
                card_info, method, card_data, hash_dist = pending_future.result()
                pending_future = None
            else:
                # First card — detect synchronously
                print(f"[{card_num}/{NUM_CARDS}] Capturing...")
                frame, card_img = _capture_and_crop(cap, bounding_corners)
                if card_img is None:
                    print("  Camera read failed!")
                    break
                cv2.imshow("Card", card_img)
                cv2.waitKey(1)
                card_info, method, card_data, hash_dist = _identify_pipeline(card_img)

            # --- Determine bin ---
            sort_cfg = get_sort_config()
            if card_info:
                bin_number = get_bin_number(card_info, current_mode, card_data=card_data)
                card_name = card_info['Name']
                set_code = card_info.get('Set', '???')
                print(f"[{card_num}/{NUM_CARDS}] {card_name} [{set_code}]  →  Bin {bin_number}  "
                      f"(dist={hash_dist:.1f})")
            else:
                bin_number = sort_cfg.fallback_bin if sort_cfg else 10
                card_name = "UNRECOGNIZED"
                set_code = ""
                print(f"[{card_num}/{NUM_CARDS}] {card_name}  →  fallback Bin {bin_number}")

            # --- Start motion ---
            _motion_busy.set()
            motion_queue.put(('pick_drop', bin_number))

            # --- Wait for motion (keep UI alive) ---
            while _motion_busy.is_set():
                key = cv2.waitKey(10) & 0xFF
                if key == 27:
                    aborted = True
                    break
                _update_live_view(cap, bounding_corners,
                                  f"Sorting {card_num}/{NUM_CARDS}...")

            if aborted:
                print("\n[aborted] ESC pressed, stopping after current card.")
                break

            # --- After card 1 motion: pause so user can reposition bin & redo bbox ---
            if card_num == 1:
                print("\n  Card 1 sorted. Reposition bin now if needed.")
                print("  SPACE=continue | b=redo bounding box | ESC=abort")
                paused = True
                while paused:
                    _update_live_view(cap, bounding_corners,
                                      "SPACE=go | b=bbox | ESC=abort")
                    key = cv2.waitKey(1) & 0xFF
                    if key == 32:  # SPACE
                        paused = False
                    elif key == ord('b'):
                        new_corners = setup_bounding_box(cap)
                        if new_corners is not None:
                            bounding_corners = new_corners
                        print("  Bounding box updated. SPACE=continue | b=redo again | ESC=abort")
                    elif key == 27:
                        aborted = True
                        paused = False
                if aborted:
                    break

            # --- Motion done — capture + start next detection in background ---
            if card_num < NUM_CARDS:
                frame, card_img = _capture_and_crop(cap, bounding_corners)
                if card_img is not None:
                    cv2.imshow("Card", card_img)
                    cv2.waitKey(1)
                    pending_future = executor.submit(_identify_pipeline,
                                                     card_img.copy())
                else:
                    print("  Camera read failed!")
                    break

            # --- Record ---
            cycle_time = time.time() - cycle_start
            results.append((card_num, card_name, set_code, bin_number,
                            hash_dist if hash_dist else 0, cycle_time))
            tracker.record_scan(
                card_info=card_info, bin_num=bin_number,
                method=method, hash_distance=hash_dist,
                card_data=card_data,
            )
            print(f"  cycle: {cycle_time:.2f}s\n")

    except KeyboardInterrupt:
        print("\n[interrupted]")
    finally:
        motion_queue.put(('stop',))
        motion_thread.join(timeout=5)
        executor.shutdown(wait=False)
        _motion_stop.set()

    return results


# ---------------------------------------------------------------------------
# Results / accuracy / logging
# ---------------------------------------------------------------------------

def _print_results(results):
    """Print a summary table of the batch."""
    print()
    print("=" * 60)
    print("  RESULTS")
    print("=" * 60)
    print(f"  {'#':<4} {'Card':<35} {'Set':<6} {'Bin':<5} {'Dist':<8} {'Time'}")
    print(f"  {'—'*4} {'—'*35} {'—'*6} {'—'*5} {'—'*8} {'—'*6}")
    for num, name, sc, bn, dist, ct in results:
        print(f"  {num:<4} {name:<35} {sc:<6} {bn:<5} {dist:<8.1f} {ct:.2f}s")

    if results:
        times = [r[5] for r in results]
        avg_time = sum(times) / len(times)
        cards_per_hour = 3600.0 / avg_time if avg_time > 0 else 0
        recognized = sum(1 for r in results if r[1] != "UNRECOGNIZED")
        print()
        print(f"  Cards sorted: {len(results)}/{NUM_CARDS}")
        print(f"  Recognized:   {recognized}/{len(results)}")
        print(f"  Avg time:     {avg_time:.2f}s/card")
        print(f"  Fastest:      {min(times):.2f}s")
        print(f"  Slowest:      {max(times):.2f}s")
        print(f"  Throughput:   ~{cards_per_hour:.0f} cards/hour")
    print("=" * 60)


def _prompt_accuracy(results):
    """
    Ask the user which cards were correctly identified.
    Returns a list of (card_num, name, correct: bool) tuples.
    """
    if not results:
        return []

    print("\nWhich cards were WRONG? Enter the card numbers separated by")
    print("spaces (e.g. '2 5 8'), or press Enter if all were correct.")
    raw = input("Wrong cards: ").strip()

    wrong_set = set()
    if raw:
        for token in raw.split():
            try:
                wrong_set.add(int(token))
            except ValueError:
                pass

    accuracy = []
    for num, name, sc, bn, dist, ct in results:
        correct = num not in wrong_set
        accuracy.append((num, name, correct))

    num_correct = sum(1 for _, _, c in accuracy if c)
    pct = 100 * num_correct / len(accuracy) if accuracy else 0
    print(f"\n  Accuracy: {num_correct}/{len(accuracy)} ({pct:.0f}%)")

    return accuracy


def _save_log(results, accuracy, mode, bin_count):
    """Save a timestamped test log to test_logs/."""
    if not results:
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(LOG_DIR, f"test_{timestamp}.txt")

    times = [r[5] for r in results]
    avg_time = sum(times) / len(times) if times else 0
    cards_per_hour = 3600.0 / avg_time if avg_time > 0 else 0
    num_correct = sum(1 for _, _, c in accuracy if c)
    pct = 100 * num_correct / len(accuracy) if accuracy else 0

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(f"10-Card Sort Test — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Mode: {mode}, Bins: {bin_count}\n")
        f.write(f"Cards: {len(results)}, "
                f"Correct: {num_correct}/{len(accuracy)} ({pct:.0f}%)\n")
        f.write(f"Avg: {avg_time:.2f}s/card, "
                f"Fastest: {min(times):.2f}s, "
                f"Slowest: {max(times):.2f}s, "
                f"~{cards_per_hour:.0f} cards/hr\n")
        f.write("\n")
        f.write(f"{'#':<4} {'Card':<35} {'Set':<6} {'Bin':<5} {'Dist':<8} {'Time':<8} {'Correct'}\n")
        f.write(f"{'-'*4} {'-'*35} {'-'*6} {'-'*5} {'-'*8} {'-'*8} {'-'*7}\n")

        acc_map = {a[0]: a[2] for a in accuracy}
        for num, name, sc, bn, dist, ct in results:
            correct = acc_map.get(num)
            mark = "YES" if correct else "NO" if correct is False else "?"
            f.write(f"{num:<4} {name:<35} {sc:<6} {bn:<5} {dist:<8.1f} {ct:<8.2f} {mark}\n")

    print(f"  Log saved: {filepath}")


# ---------------------------------------------------------------------------
# Sort mode selection
# ---------------------------------------------------------------------------

def select_sort_mode():
    """Prompt for sorting mode."""
    print("\n" + "=" * 50)
    print("SELECT SORTING MODE")
    print("=" * 50)
    for key in sorted(SORTING_MODES.keys()):
        print(f"  {key} - {SORTING_MODES[key]}")
    print()

    choice = input("Enter mode number (or Enter for color): ").strip()
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
    print(f"\n{'=' * 60}")
    print(f"  10-CARD SORT TEST (pipelined)")
    print(f"{'=' * 60}\n")

    # --- Connect ---
    print("[setup] Connecting to control board...")
    connect_to_board()
    if not is_connected():
        print("[setup] ERROR: Could not connect. Exiting.")
        return

    # --- Home ---
    print("[setup] Homing all axes...")
    home_all()
    print("[setup] Moving to detection position...")
    move_to_detection_position()

    # --- Camera ---
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Cannot open webcam.")
        close_connection()
        return

    # --- Bounding box ---
    bounding_corners = load_bounding_box()
    if bounding_corners is not None:
        print("Using saved bounding box. Press 'b' during preview to reconfigure.")
    else:
        print("No saved bounding box. Click 4 corners to set one.")
        bounding_corners = setup_bounding_box(cap)
        if bounding_corners is None:
            print("Cancelled.")
            cap.release()
            close_connection()
            return

    # --- Sort mode ---
    current_mode = select_sort_mode()
    sort_cfg = get_sort_config()
    bin_count = sort_cfg.bin_count if sort_cfg else 10
    configure_bins(bin_count)

    # --- Tracker ---
    tracker = ScanTracker()
    config_name = getattr(sort_cfg, '_config_name', None) if sort_cfg else None
    tracker.start_session(
        sort_mode=current_mode,
        config_name=config_name,
        bin_count=bin_count,
    )

    cv2.namedWindow("Live View", cv2.WINDOW_AUTOSIZE)

    # --- Main loop: run batches until user quits ---
    batch_num = 0
    while True:
        batch_num += 1

        # --- Preview / wait for SPACE ---
        label = f"Batch {batch_num}: {NUM_CARDS} cards"
        print(f"\n{label}")
        print(f"Mode: {current_mode}, Bins: {bin_count}")
        print("Press SPACE to start, 'b' to redo bounding box, ESC to quit.\n")

        started = False
        while not started:
            ret, frame = cap.read()
            if not ret:
                break
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
            disp = frame.copy()
            if bounding_corners is not None:
                pts = bounding_corners.astype(int)
                for i in range(4):
                    cv2.line(disp, tuple(pts[i]), tuple(pts[(i + 1) % 4]),
                             (0, 255, 0), 2)
            h, w = disp.shape[:2]
            cv2.putText(disp, "SPACE=start | b=bbox | ESC=quit", (10, h - 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)
            cv2.putText(disp, f"Mode: {current_mode}", (w - 250, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
            cv2.imshow("Live View", disp)

            key = cv2.waitKey(1) & 0xFF
            if key == 27:
                # Quit
                print("\n[shutdown] Pumps off, Z to top...")
                all_pumps_off()
                z_to_top()
                cap.release()
                cv2.destroyAllWindows()
                tracker.print_status()
                tracker.end_session()
                close_connection()
                print("[shutdown] Done.")
                return
            elif key == ord('b'):
                bounding_corners = setup_bounding_box(cap)
                if bounding_corners is None:
                    bounding_corners = load_bounding_box()
            elif key == 32:  # SPACE
                started = True

        # --- Run the batch ---
        print(f"\n{'=' * 60}")
        print(f"  SORTING {NUM_CARDS} CARDS  (batch {batch_num})")
        print(f"{'=' * 60}\n")

        results = _run_batch(cap, bounding_corners, current_mode, tracker)

        # --- Show results, ask for accuracy, save log ---
        _print_results(results)
        accuracy = _prompt_accuracy(results)
        _save_log(results, accuracy, current_mode, bin_count)

        # Loop back to preview for next batch (or ESC to quit)


if __name__ == "__main__":
    main()
