# test_detection_live.py
# ---------------------------------------------------------------------------
# Interactive test for the full detection pipeline with color correction.
# Runs hash matching BOTH with and without color correction on every scan
# so you can directly compare accuracy.
#
# Controls:
#   SPACE  = capture and process the current frame
#   B      = set up bounding box (click 4 corners)
#   D      = toggle debug mode (shows ROI, contour, correction values)
#   ESC    = quit
# ---------------------------------------------------------------------------

import cv2
import numpy as np
from PIL import Image

print("Loading card data and hashes, please wait...")

from config import (
    CARD_WIDTH, CARD_HEIGHT, CROP_SIZE,
    PHASH_DISTANCE_THRESHOLD, PHASH_CLOSE_MATCH_DIFF, EXCLUDED_SETS,
)
from detection import (
    setup_bounding_box,
    load_bounding_box,
    get_perspective_corrected_card,
    find_card_contour_no_bg,
    deskew_card,
    color_correct_card,
)
from detection import determine_orientation
from cards import extract_card_info, CARD_DATA_BY_ID
from hashing import compute_distances_for_image, compute_combined_distances


def hash_identify(card_img, label=""):
    """
    Run hash matching on a card image. Returns (card_info, top_dist, top5).
    top5 is a list of (name, set, distance) for display.
    """
    cropped = card_img[0:CROP_SIZE, 0:CROP_SIZE]
    img_pil = Image.fromarray(cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB))
    all_dists = compute_distances_for_image(img_pil, hash_size=16)

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
        return None, None, []

    top_id, top_dist = allowed[0]

    top5 = []
    for cid, dist in allowed[:5]:
        cname = CARD_DATA_BY_ID.get(cid, {}).get('name', '?')
        cset = CARD_DATA_BY_ID.get(cid, {}).get('set', '?')
        top5.append((cname, cset, dist))

    if top_dist > PHASH_DISTANCE_THRESHOLD:
        return None, top_dist, top5

    info = extract_card_info(top_id)
    return info, top_dist, top5


def main():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[ERROR] Could not open webcam.")
        return

    bounding_corners = load_bounding_box()
    if bounding_corners is not None:
        print("[setup] Loaded saved bounding box.")
    else:
        print("[setup] No bounding box found. Press 'B' to set one.")

    debug_mode = False
    scan_count = 0

    print("\n--- Controls ---")
    print("  SPACE = capture & process (runs matching WITH and WITHOUT correction)")
    print("  B     = set bounding box")
    print("  D     = toggle debug mode")
    print("  ESC   = quit")
    print("----------------\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

        # Draw bounding box overlay
        display = frame.copy()
        if bounding_corners is not None:
            pts = bounding_corners.astype(int)
            for i in range(4):
                cv2.line(display, tuple(pts[i]), tuple(pts[(i + 1) % 4]),
                         (0, 255, 0), 2)

        cv2.imshow("Webcam", display)

        key = cv2.waitKey(1) & 0xFF
        if key == 27:  # ESC
            break
        elif key == ord('b'):
            bounding_corners = setup_bounding_box(cap)
            if bounding_corners is None:
                bounding_corners = load_bounding_box()
        elif key == ord('d'):
            debug_mode = not debug_mode
            print(f"[test] Debug mode: {'ON' if debug_mode else 'OFF'}")
        elif key == 32:  # SPACE
            if bounding_corners is None:
                print("[test] No bounding box. Press 'B' to set one.")
                continue

            scan_count += 1
            print(f"\n{'=' * 70}")
            print(f"  SCAN #{scan_count}")
            print(f"{'=' * 70}")

            # -------------------------------------------------------
            # Step 1: Dynamic contour detection within ROI
            # -------------------------------------------------------
            pts = bounding_corners.reshape(-1, 2).astype(np.float32)
            x_min, y_min = pts.min(axis=0)
            x_max, y_max = pts.max(axis=0)
            box_w = x_max - x_min
            box_h = y_max - y_min
            margin = 0.20
            pad_x = box_w * margin
            pad_y = box_h * margin
            h_frame, w_frame = frame.shape[:2]
            roi_x1 = max(0, int(x_min - pad_x))
            roi_y1 = max(0, int(y_min - pad_y))
            roi_x2 = min(w_frame, int(x_max + pad_x))
            roi_y2 = min(h_frame, int(y_max + pad_y))
            roi = frame[roi_y1:roi_y2, roi_x1:roi_x2]

            roi_area = (roi_x2 - roi_x1) * (roi_y2 - roi_y1)
            min_card_area = roi_area * 0.25
            expected_ratio = max(box_w, box_h) / (min(box_w, box_h) + 1e-6)

            contour = find_card_contour_no_bg(
                roi, min_area=min_card_area,
                expected_ratio=expected_ratio,
                debug=debug_mode
            )

            if contour is not None:
                contour_shifted = contour.copy()
                contour_shifted[:, :, 0] += roi_x1
                contour_shifted[:, :, 1] += roi_y1
                print(f"[detect] Card contour found dynamically")

                # Show detected contour vs saved box
                contour_display = frame.copy()
                cv2.drawContours(contour_display, [contour_shifted], -1,
                                 (0, 255, 0), 2)
                bb_pts = bounding_corners.astype(int)
                for i in range(4):
                    cv2.line(contour_display, tuple(bb_pts[i]),
                             tuple(bb_pts[(i + 1) % 4]), (255, 0, 0), 2)
                cv2.imshow("Contour (green=detected, blue=saved box)",
                           contour_display)

                card_img = get_perspective_corrected_card(
                    frame, contour_shifted,
                    width=CARD_WIDTH, height=CARD_HEIGHT
                )
            else:
                print(f"[detect] No contour — using saved bounding box")
                approx = bounding_corners.reshape((4, 1, 2)).astype(np.float32)
                card_img = get_perspective_corrected_card(
                    frame, approx,
                    width=CARD_WIDTH, height=CARD_HEIGHT
                )

            # -------------------------------------------------------
            # Step 2: Deskew
            # -------------------------------------------------------
            card_img = deskew_card(card_img)

            # -------------------------------------------------------
            # Step 3: Orientation detection
            # -------------------------------------------------------
            card_img, was_rotated = determine_orientation(card_img)
            if was_rotated:
                print(f"[detect] Card was upside down — rotated 180°")

            # -------------------------------------------------------
            # Step 4: Run hash matching WITHOUT color correction
            # -------------------------------------------------------
            uncorrected = card_img.copy()
            print(f"\n--- WITHOUT color correction ---")
            info_raw, dist_raw, top5_raw = hash_identify(uncorrected)

            if info_raw:
                sets_raw = info_raw.get('Sets', [info_raw.get('Set', '?')])
                print(f"  Result: {info_raw['Name']}")
                print(f"  Sets:   {', '.join(sets_raw)}")
                print(f"  Dist:   {dist_raw:.2f}")
            else:
                print(f"  Result: UNRECOGNIZED")
                if dist_raw is not None:
                    print(f"  Dist:   {dist_raw:.2f} (above threshold {PHASH_DISTANCE_THRESHOLD})")

            print(f"  Top 5:")
            for rank, (name, sset, dist) in enumerate(top5_raw, 1):
                print(f"    #{rank}: {name} ({sset}) dist={dist:.2f}")

            # -------------------------------------------------------
            # Step 5: Color correction
            # -------------------------------------------------------
            print(f"\n--- Color correction ---")
            corrected = color_correct_card(card_img, debug=True)

            # -------------------------------------------------------
            # Step 6: Run hash matching WITH color correction
            # -------------------------------------------------------
            print(f"\n--- WITH color correction ---")
            info_cc, dist_cc, top5_cc = hash_identify(corrected)

            if info_cc:
                sets_cc = info_cc.get('Sets', [info_cc.get('Set', '?')])
                print(f"  Result: {info_cc['Name']}")
                print(f"  Sets:   {', '.join(sets_cc)}")
                print(f"  Dist:   {dist_cc:.2f}")
            else:
                print(f"  Result: UNRECOGNIZED")
                if dist_cc is not None:
                    print(f"  Dist:   {dist_cc:.2f} (above threshold {PHASH_DISTANCE_THRESHOLD})")

            print(f"  Top 5:")
            for rank, (name, sset, dist) in enumerate(top5_cc, 1):
                print(f"    #{rank}: {name} ({sset}) dist={dist:.2f}")

            # -------------------------------------------------------
            # Step 7: Comparison summary
            # -------------------------------------------------------
            print(f"\n--- COMPARISON ---")
            name_raw = info_raw['Name'] if info_raw else "UNRECOGNIZED"
            name_cc = info_cc['Name'] if info_cc else "UNRECOGNIZED"
            same_result = name_raw == name_cc

            if dist_raw is not None and dist_cc is not None:
                improvement = dist_raw - dist_cc
                print(f"  Without CC: {name_raw} (dist={dist_raw:.2f})")
                print(f"  With CC:    {name_cc} (dist={dist_cc:.2f})")
                if improvement > 0:
                    print(f"  >> Color correction IMPROVED distance by {improvement:.2f}")
                elif improvement < 0:
                    print(f"  >> Color correction WORSENED distance by {-improvement:.2f}")
                else:
                    print(f"  >> No change in distance")
                if not same_result:
                    print(f"  >> WARNING: Different cards matched!")
            elif dist_raw is None and dist_cc is not None:
                print(f"  Without CC: UNRECOGNIZED")
                print(f"  With CC:    {name_cc} (dist={dist_cc:.2f})")
                print(f"  >> Color correction RESCUED a failed match!")
            elif dist_raw is not None and dist_cc is None:
                print(f"  Without CC: {name_raw} (dist={dist_raw:.2f})")
                print(f"  With CC:    UNRECOGNIZED")
                print(f"  >> Color correction BROKE a working match!")
            else:
                print(f"  Both unrecognized")

            # -------------------------------------------------------
            # Step 8: Display images
            # -------------------------------------------------------
            scale = 0.5
            disp_w = int(CARD_WIDTH * scale)
            disp_h = int(CARD_HEIGHT * scale)
            before_small = cv2.resize(uncorrected, (disp_w, disp_h))
            after_small = cv2.resize(corrected, (disp_w, disp_h))

            # Add result text to images
            def put_result(img, name, dist, x_off=0):
                color = (0, 255, 0) if dist and dist <= PHASH_DISTANCE_THRESHOLD else (0, 0, 255)
                cv2.putText(img, name[:30], (10 + x_off, disp_h - 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
                if dist is not None:
                    cv2.putText(img, f"dist={dist:.1f}", (10 + x_off, disp_h - 15),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

            put_result(before_small, name_raw, dist_raw)
            put_result(after_small, name_cc, dist_cc)

            side_by_side = np.hstack([before_small, after_small])
            cv2.putText(side_by_side, "WITHOUT correction", (10, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            cv2.putText(side_by_side, "WITH correction", (disp_w + 10, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

            cv2.imshow("Comparison", side_by_side)


            print(f"\n[test] Press SPACE to scan again.")
            print(f"{'=' * 70}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
