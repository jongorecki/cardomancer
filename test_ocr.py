# test_ocr.py
# ---------------------------------------------------------------------------
# Test script for card detection without physical hardware.
# Uses webcam + click bounding box + hash identification (primary)
# with optional OCR for disambiguation.
# ---------------------------------------------------------------------------

import cv2
import numpy as np
from PIL import Image

print("Loading files, please wait...")

from config import CROP_SIZE, PHASH_DISTANCE_THRESHOLD, PHASH_CLOSE_MATCH_DIFF
from config import EXCLUDED_SETS, TITLE_REGION, COLLECTOR_REGION
from card_lookup import lookup_by_name, lookup_by_set_collector, get_card_info_from_data
from ocr import ocr_title, ocr_collector, parse_collector_info, crop_region, determine_orientation
from detection import setup_bounding_box, load_bounding_box, crop_to_bounding_box
from cards import extract_card_info, CARD_DATA_BY_ID
from hashing import compute_distances_for_image, compute_combined_distances
from sorting import get_bin_number


def identify_card(card_img):
    """
    Identify a card using hash matching (primary) with OCR disambiguation.
    Returns (card_info, method_str) or (None, None).
    """
    # --- Hash identification ---
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

    # Check if match is ambiguous
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

    # --- OCR disambiguation for ambiguous hash matches ---
    print("[ocr] Hash ambiguous, trying OCR to disambiguate...")

    # Try OCR on collector info (set code + number is most reliable)
    collector_text = ocr_collector(card_img)
    set_code, collector_num = parse_collector_info(collector_text)
    print(f"[ocr] Collector: '{collector_text}' -> set={set_code}, num={collector_num}")

    if set_code and collector_num:
        card = lookup_by_set_collector(set_code, collector_num)
        if card:
            info = get_card_info_from_data(card)
            print(f"[ocr] Disambiguated by set/collector: {info['Name']}")
            return info, "hash+ocr_exact", card

    # Try OCR on title
    title_text = ocr_title(card_img)
    print(f"[ocr] Title: '{title_text}'")
    if title_text:
        matched_name, matched_cards = lookup_by_name(title_text)
        if matched_name:
            # See if any of the top hash candidates match the OCR name
            for cid, dist in allowed[:10]:
                cname = CARD_DATA_BY_ID.get(cid, {}).get('name', '')
                if cname.lower() == matched_name.lower():
                    info = extract_card_info(cid)
                    return info, "hash+ocr_name", CARD_DATA_BY_ID.get(cid)

    # OCR didn't help, just use top hash match
    info = extract_card_info(top_id)
    card_data = CARD_DATA_BY_ID.get(top_id)
    return info, "hash_ambiguous", card_data


def main():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[test] Cannot open webcam.")
        return

    # Set up bounding box
    bounding_corners = load_bounding_box()
    if bounding_corners is not None:
        print("[test] Using saved bounding box. Press 'b' to reconfigure.")
    else:
        print("[test] No saved bounding box. Please click 4 corners.")
        bounding_corners = setup_bounding_box(cap)
        if bounding_corners is None:
            print("[test] Cancelled.")
            cap.release()
            return

    print("[test] Press SPACE to detect. Press 'b' to reset bounding box. Press ESC to quit.")

    cv2.namedWindow("Webcam", cv2.WINDOW_AUTOSIZE)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

        # Draw bounding box
        display = frame.copy()
        if bounding_corners is not None:
            pts = bounding_corners.astype(int)
            for i in range(4):
                cv2.line(display, tuple(pts[i]), tuple(pts[(i+1) % 4]),
                         (0, 255, 0), 2)

        cv2.imshow("Webcam", display)

        key = cv2.waitKey(1) & 0xFF
        if key == 27:
            break
        elif key == ord('b'):
            bounding_corners = setup_bounding_box(cap)
            if bounding_corners is None:
                bounding_corners = load_bounding_box()
        elif key == 32:  # SPACE
            if bounding_corners is None:
                print("[test] No bounding box. Press 'b' to set one.")
                continue

            print("\n" + "=" * 60)
            print("[test] Detecting card...")

            # Crop to bounding box
            card_img = crop_to_bounding_box(frame, bounding_corners)
            cv2.imshow("Card (raw crop)", card_img)

            # Determine orientation via hash comparison
            card_img, was_rotated = determine_orientation(card_img)
            print(f"[test] Rotated 180: {was_rotated}")
            cv2.imshow("Card (oriented)", card_img)

            # Identify
            card_info, method, card_data = identify_card(card_img)

            if card_info:
                sets = card_info.get('Sets', [card_info['Set']])
                print(f"\n>>> RESULT: {card_info['Name']} (method={method})")
                print(f"    Sets: {', '.join(sets)}")
                print(f"    {card_info}")
            else:
                print("\n>>> RESULT: Unrecognized")

            print("=" * 60)

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
