# test_staging_detection.py
# ---------------------------------------------------------------------------
# Test script for white-background staged detection.
#
# Flow:
#   1. Home, pick first card from source, drop on white staging area, park
#   2. User positions camera → press SPACE
#   3. For each card:
#      a. Capture frame → contour-detect on white bg → hash identify
#      b. Pick card from staging area → drop in drop zone
#      c. Pick next card from source → drop on staging area → park
#      d. Pause — press SPACE for next card
#   4. After all cards: prompt which were wrong, save log
# ---------------------------------------------------------------------------

import os
import sys
import time
import threading
import cv2
import numpy as np

print("Loading files, please wait...")

from config import (
    PHASH_DISTANCE_THRESHOLD, PHASH_CLOSE_MATCH_DIFF,
    EXCLUDED_SETS,
)
from detection import detect_card_on_staging, detect_card_layout, determine_orientation
from cards import extract_card_info, CARD_DATA_BY_ID
from hashing import compute_combined_distances_with_rerank, is_card_back
from gcode_control import (
    connect_to_board,
    close_connection,
    is_connected,
    home_all,
    pick_from_position,
    drop_on_surface,
    drop_on_staging,
    pick_from_staging,
    move_to_camera_position,
    park_for_camera,
    wait_for_completion,
    all_pumps_off,
    z_to_top,
    X_SOURCE_BIN,
    X_STAGING_POSITION,
    X_CAMERA_PARK,
)

NUM_CARDS = 10
X_DROP_ZONE = 400.0
LOG_DIR = os.path.join(os.path.dirname(__file__), "test_logs")


# ---------------------------------------------------------------------------
# Card identification (same as test_10_cards.py)
# ---------------------------------------------------------------------------

def identify_card(card_img, layout="normal"):
    """
    Identify a card using hash matching.
    Returns (card_info, method_str, card_data, hash_distance)
    or (None, None, None, None).
    """
    images_dir = os.path.join(os.path.dirname(__file__), "downloaded_cards")
    all_dists = compute_combined_distances_with_rerank(
        card_img, hash_size=16, rerank_top=30, images_dir=images_dir,
        layout=layout
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


LAYOUT_RETRY_THRESHOLD = 60  # If best match > this, try alternate layouts


def _identify_pipeline(card_img):
    """Card back check + orientation check + layout detection + identification.
    If the first layout gives a low-confidence match, retries with alternate layouts."""
    back_detected, back_dist = is_card_back(card_img)
    print(f"  Card back check: dist={back_dist:.1f} (threshold={40}, detected={back_detected})")
    if back_detected:
        print(f"  CARD BACK detected (dist={back_dist:.1f})")
        return None, "card_back", None, back_dist

    card_img, was_rotated = determine_orientation(card_img)
    if was_rotated:
        print("  (Card was upside-down, rotated 180)")

    # Detect card layout (saga/class/normal) for layout-aware art cropping
    layout = detect_card_layout(card_img)
    if layout != "normal":
        print(f"  Layout detected: {layout}")

    info, method, card_data, dist = identify_card(card_img, layout=layout)

    # If low confidence, try alternate layouts as fallback
    if dist is not None and dist > LAYOUT_RETRY_THRESHOLD:
        alt_layouts = [l for l in ("normal", "saga", "class") if l != layout]
        for alt in alt_layouts:
            print(f"  Low confidence (dist={dist:.1f}), retrying with layout={alt}...")
            alt_info, alt_method, alt_data, alt_dist = identify_card(card_img, layout=alt)
            if alt_dist is not None and alt_dist < dist:
                info, method, card_data, dist = alt_info, alt_method, alt_data, alt_dist
                print(f"  Better match with {alt} layout (dist={alt_dist:.1f})")
                break

    return info, method, card_data, dist


# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

def _show_live(cap, overlay_text=None):
    """Read one frame, show it with optional overlay. Returns the rotated frame."""
    ret, frame = cap.read()
    if not ret:
        return None
    frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    disp = frame.copy()
    if overlay_text:
        cv2.putText(disp, overlay_text, (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    cv2.imshow("Live View", disp)
    return frame


def _flush_camera_buffer(cap, num_frames=15):
    """
    Flush stale frames from the camera buffer by reading and discarding them.
    Updates the live view on each frame. OpenCV buffers several frames
    internally, so we need to read enough to ensure we get a fresh one.
    """
    frame = None
    for _ in range(num_frames):
        frame = _show_live(cap, "Waiting for clear view...")
        cv2.waitKey(1)
    return frame


def _capture_detection_frame(cap):
    """
    Flush the camera buffer to clear stale frames (e.g. showing carriage),
    then return a fresh frame for detection.
    """
    frame = _flush_camera_buffer(cap, num_frames=15)
    return frame


def _run_motion_with_live_view(cap, motion_func, *args, overlay_text="Moving..."):
    """
    Run a blocking motion function in a background thread while keeping
    the live camera view updating on the main thread.
    """
    done = threading.Event()

    def _worker():
        motion_func(*args)
        done.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    while not done.is_set():
        _show_live(cap, overlay_text)
        key = cv2.waitKey(30) & 0xFF
        if key == 27:
            # Can't abort motion mid-move, but flag it
            pass
    t.join()


# ---------------------------------------------------------------------------
# Results logging
# ---------------------------------------------------------------------------

def _print_results(results):
    """Print a summary table."""
    print("\n" + "=" * 70)
    print("  STAGING DETECTION TEST RESULTS")
    print("=" * 70)
    print(f"  {'#':<4} {'Card':<35} {'Set':<6} {'Dist':<8} {'Time'}")
    print(f"  {'—'*4} {'—'*35} {'—'*6} {'—'*8} {'—'*6}")
    for num, name, sc, dist, ct in results:
        print(f"  {num:<4} {name:<35} {sc:<6} {dist:<8.1f} {ct:.2f}s")

    if results:
        times = [r[4] for r in results]
        avg_time = sum(times) / len(times)
        cards_per_hour = 3600.0 / avg_time if avg_time > 0 else 0
        recognized = sum(1 for r in results if r[1] != "UNRECOGNIZED")
        print()
        print(f"  Cards tested: {len(results)}/{NUM_CARDS}")
        print(f"  Recognized:   {recognized}/{len(results)}")
        print(f"  Avg time:     {avg_time:.2f}s/card")
        if len(times) > 1:
            print(f"  Fastest:      {min(times):.2f}s")
            print(f"  Slowest:      {max(times):.2f}s")
        print(f"  Throughput:   ~{cards_per_hour:.0f} cards/hour")
    print("=" * 70)


def _prompt_accuracy(results):
    """Ask which cards were wrong. Returns list of (num, name, correct)."""
    if not results:
        return []

    print("\nWhich cards were WRONG? Enter card numbers separated by spaces")
    print("(e.g. '2 5 8'), or press Enter if all were correct.")
    raw = input("Wrong cards: ").strip()

    wrong_set = set()
    if raw:
        for token in raw.split():
            try:
                wrong_set.add(int(token))
            except ValueError:
                pass

    accuracy = []
    for num, name, sc, dist, ct in results:
        correct = num not in wrong_set
        accuracy.append((num, name, correct))

    num_correct = sum(1 for _, _, c in accuracy if c)
    pct = 100 * num_correct / len(accuracy) if accuracy else 0
    print(f"\n  Accuracy: {num_correct}/{len(accuracy)} ({pct:.0f}%)")
    return accuracy


def _save_log(results, accuracy):
    """Save a timestamped test log."""
    if not results:
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(LOG_DIR, f"staging_test_{timestamp}.txt")

    times = [r[4] for r in results]
    avg_time = sum(times) / len(times) if times else 0
    cards_per_hour = 3600.0 / avg_time if avg_time > 0 else 0
    num_correct = sum(1 for _, _, c in accuracy if c)
    pct = 100 * num_correct / len(accuracy) if accuracy else 0

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(f"Staging Detection Test — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Mode: white background staging\n")
        f.write(f"Source: X={X_SOURCE_BIN}, Stage: X={X_STAGING_POSITION}, "
                f"Drop: X={X_DROP_ZONE}\n")
        f.write(f"Cards: {len(results)}, "
                f"Correct: {num_correct}/{len(accuracy)} ({pct:.0f}%)\n")
        f.write(f"Avg: {avg_time:.2f}s/card, ~{cards_per_hour:.0f} cards/hr\n")
        f.write("\n")
        f.write(f"{'#':<4} {'Card':<35} {'Set':<6} {'Dist':<8} "
                f"{'Time':<8} {'Correct'}\n")
        f.write(f"{'-'*4} {'-'*35} {'-'*6} {'-'*8} {'-'*8} {'-'*7}\n")

        acc_map = {a[0]: a[2] for a in accuracy}
        for num, name, sc, dist, ct in results:
            correct = acc_map.get(num)
            mark = "YES" if correct else "NO" if correct is False else "?"
            f.write(f"{num:<4} {name:<35} {sc:<6} {dist:<8.1f} "
                    f"{ct:<8.2f} {mark}\n")

    print(f"  Log saved: {filepath}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # --- Connect to board ---
    print("\n=== Staging Detection Test ===")
    print(f"Source bin: X={X_SOURCE_BIN}")
    print(f"Staging area: X={X_STAGING_POSITION}")
    print(f"Drop zone: X={X_DROP_ZONE}")
    print(f"Camera park: X={X_CAMERA_PARK}")
    print(f"Cards to test: {NUM_CARDS}\n")

    connect_to_board()
    if not is_connected():
        print("ERROR: Could not connect to board. Exiting.")
        return

    # --- Open camera ---
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("ERROR: Could not open camera. Exiting.")
        close_connection()
        return

    results = []

    try:
        # --- Home ---
        print("Homing all axes...")
        home_all()

        # --- Stage first card ---
        print("\nPicking first card from source bin...")
        _run_motion_with_live_view(cap, pick_from_position, X_SOURCE_BIN,
                                   overlay_text="Picking from source...")
        print("Dropping on staging area...")
        _run_motion_with_live_view(cap, drop_on_staging, 1,
                                   overlay_text="Dropping on stage...")
        print("Moving to camera position...")
        _run_motion_with_live_view(cap, move_to_camera_position,
                                   overlay_text="Moving to camera pos...")

        # --- Wait for user to verify camera view ---
        print("\n" + "=" * 50)
        print("  Card is on the staging area.")
        print("  Camera should be viewing the card.")
        print("  Press SPACE when ready to start.")
        print("  Press ESC to abort.")
        print("=" * 50)

        ready = False
        while not ready:
            _show_live(cap, "Verify camera view → SPACE to start")
            key = cv2.waitKey(1) & 0xFF
            if key == 32:  # SPACE
                ready = True
            elif key == 27:  # ESC
                print("Aborted.")
                return

        # --- Main detection loop ---
        # New flow per card:
        #   1. Card is on staging, carriage at camera position
        #   2. Capture frame + detect contour
        #   3. Start picking card from staging (motion in background)
        #   4. While picking, run hash identification in parallel
        #   5. Once pickup done + ID done, move to drop zone
        #   6. Pick next card from source, drop on staging
        #   7. Move to camera position for next card

        for card_num in range(1, NUM_CARDS + 1):
            cycle_start = time.time()

            # --- Step 1: Capture frame at camera position ---
            wait_for_completion()
            print(f"\n[{card_num}/{NUM_CARDS}] Capturing...")
            frame = _capture_detection_frame(cap)
            if frame is None:
                print("  Camera read failed!")
                break

            # --- Step 2: Detect card contour (fast, ~50ms) ---
            card_img = detect_card_on_staging(frame, debug=False)

            if card_img is None:
                print("  No card contour found on white background!")
                card_name = "NO_CONTOUR"
                set_code = ""
                hash_dist = 0

                # Still need to pick up and move the card
                _run_motion_with_live_view(cap, pick_from_staging,
                                           overlay_text=f"Picking from stage [{card_num}]")
                _run_motion_with_live_view(cap, drop_on_surface, X_DROP_ZONE,
                                           overlay_text=f"Dropping [{card_num}]")
            else:
                cv2.imshow("Card", card_img)
                cv2.waitKey(1)

                # --- Step 3: Start pickup while identifying in parallel ---
                id_result = [None]  # mutable container for thread result

                def _identify_in_background(img):
                    id_result[0] = _identify_pipeline(img)

                id_thread = threading.Thread(
                    target=_identify_in_background, args=(card_img,), daemon=True)
                id_thread.start()

                # Pick card from staging while ID runs
                _run_motion_with_live_view(cap, pick_from_staging,
                                           overlay_text=f"Picking+identifying [{card_num}]")

                # Wait for ID to finish (likely already done)
                id_thread.join()
                card_info, method, card_data, hash_dist = id_result[0]

                if method == "card_back":
                    card_name = "CARD_BACK"
                    set_code = ""
                    print(f"[{card_num}/{NUM_CARDS}] {card_name}  "
                          f"(back dist={hash_dist:.1f})")
                elif card_info:
                    card_name = card_info['Name']
                    set_code = card_info.get('Set', '???')
                    print(f"[{card_num}/{NUM_CARDS}] {card_name} [{set_code}]  "
                          f"(dist={hash_dist:.1f})")
                else:
                    card_name = "UNRECOGNIZED"
                    set_code = ""
                    print(f"[{card_num}/{NUM_CARDS}] {card_name}  "
                          f"(dist={hash_dist:.1f})" if hash_dist else
                          f"[{card_num}/{NUM_CARDS}] {card_name}")

                # --- Step 4: Drop at drop zone ---
                _run_motion_with_live_view(cap, drop_on_surface, X_DROP_ZONE,
                                           overlay_text=f"Dropping [{card_num}]")

            # --- Record ---
            cycle_time = time.time() - cycle_start
            results.append((card_num, card_name, set_code,
                            hash_dist if hash_dist else 0, cycle_time))
            print(f"  cycle: {cycle_time:.2f}s")

            # --- Stage next card (if not last) ---
            if card_num < NUM_CARDS:
                print(f"  Staging next card...")
                _run_motion_with_live_view(cap, pick_from_position, X_SOURCE_BIN,
                                           overlay_text="Picking from source...")
                _run_motion_with_live_view(cap, drop_on_staging, card_num + 1,
                                           overlay_text="Dropping on stage...")
                _run_motion_with_live_view(cap, move_to_camera_position,
                                           overlay_text="Moving to camera pos...")

                # Check for ESC abort
                key = cv2.waitKey(1) & 0xFF
                if key == 27:
                    print("\n[aborted] Stopping after current card.")
                    break

    except KeyboardInterrupt:
        print("\n[interrupted]")
    finally:
        # Safety: pumps off, Z up
        all_pumps_off()
        z_to_top()
        wait_for_completion()

    # --- Results ---
    _print_results(results)
    accuracy = _prompt_accuracy(results)
    _save_log(results, accuracy)

    # --- Cleanup ---
    cap.release()
    cv2.destroyAllWindows()
    close_connection()
    print("\nDone.")


if __name__ == "__main__":
    main()
