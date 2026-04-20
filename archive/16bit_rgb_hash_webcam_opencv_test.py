import cv2
import numpy as np
import json
from PIL import Image
import imagehash
import os

# Constants
MAX_DISTANCE_THRESHOLD = 75  # Updated for new error handling rules
CROP_SIZE = 745  # Reverted to original value for accurate detection
WIDTH = 745      # Defined globally for use in main()
HEIGHT = 1043    # Defined globally for use in main()

# Paths
hash_db_path = "card_hashes.json"
cards_json_path = r"C:\Users\Jon\Downloads\default-cards-20241206100658.json"

# Load hash database
if os.path.exists(hash_db_path):
    with open(hash_db_path, 'r', encoding='utf-8') as f:
        hash_db = json.load(f)
else:
    hash_db = {}

# Load card info
if os.path.exists(cards_json_path):
    with open(cards_json_path, 'r', encoding='utf-8') as f:
        cards_data = json.load(f)
    card_data_by_id = {c.get('id'): c for c in cards_data}
else:
    cards_data = []
    card_data_by_id = {}

# Precompute hash objects for faster lookup
precomputed_hashes = []
for card_id, h in hash_db.items():
    r_phash_str = h.get('r_phash')
    g_phash_str = h.get('g_phash')
    b_phash_str = h.get('b_phash')
    if r_phash_str and g_phash_str and b_phash_str:
        try:
            stored_r = imagehash.hex_to_hash(r_phash_str)
            stored_g = imagehash.hex_to_hash(g_phash_str)
            stored_b = imagehash.hex_to_hash(b_phash_str)
            precomputed_hashes.append((card_id, stored_r, stored_g, stored_b))
        except ValueError:
            print(f"Invalid hash format for card {card_id}. Skipping.")
            continue

# Precompute minimum price by illustration_id, excluding specific sets
min_price_by_illustration_id = {}
excluded_sets = {"30a", "lea", "leb", "ced", "cei"}  # Sets to exclude

for c in cards_data:
    illustration_id = c.get('illustration_id')
    set_code = c.get('set')

    # Skip cards without an illustration_id or from excluded sets
    if not illustration_id or set_code in excluded_sets:
        continue

    usd_price = c.get('prices', {}).get('usd')
    card_back_id = c.get('card_back_id')
    
    # Skip if card_back_id is not the specified one
    if card_back_id != "0aeebaf5-8c7d-4636-9e82-8c27447861f7":
        continue

    if usd_price:
        try:
            p = float(usd_price)
            # Only update if this card has a lower price
            if illustration_id not in min_price_by_illustration_id or p < min_price_by_illustration_id[illustration_id]:
                min_price_by_illustration_id[illustration_id] = p
        except ValueError:
            continue

# Filter out excluded sets during the main processing
filtered_cards = []
for c in cards_data:
    illustration_id = c.get('illustration_id')
    set_code = c.get('set')

    # Skip cards from excluded sets
    if not illustration_id or set_code in excluded_sets:
        continue

    # Check if this card matches the minimum price for its illustration_id
    usd_price = c.get('prices', {}).get('usd')
    if usd_price:
        try:
            p = float(usd_price)
            if illustration_id in min_price_by_illustration_id and p == min_price_by_illustration_id[illustration_id]:
                filtered_cards.append(c)
        except ValueError:
            continue

def hash_image_color(img_pil, hash_size=16):
    """
    Compute phash for each R, G, B channel with specified hash_size and compare against precomputed_hashes.
    Returns (best_id, best_distance).
    """
    img_pil = img_pil.convert('RGB')
    r, g, b = img_pil.split()

    r_ph = imagehash.phash(r, hash_size=hash_size)
    g_ph = imagehash.phash(g, hash_size=hash_size)
    b_ph = imagehash.phash(b, hash_size=hash_size)

    best_id = None
    best_dist = float('inf')

    for (card_id, stored_r, stored_g, stored_b) in precomputed_hashes:
        dist_r = r_ph - stored_r
        dist_g = g_ph - stored_g
        dist_b = b_ph - stored_b

        dist = (dist_r + dist_g + dist_b) / 3.0

        if dist < best_dist:
            best_dist = dist
            best_id = card_id

    return best_id, best_dist

def find_best_color_matches(img_pil_upright, img_pil_rotated):
    # Single-threaded hashing for speed with increased hash_size
    u_comb_id, u_comb_dist = hash_image_color(img_pil_upright, hash_size=16)
    r_comb_id, r_comb_dist = hash_image_color(img_pil_rotated, hash_size=16)
    return u_comb_id, u_comb_dist, r_comb_id, r_comb_dist

def find_card_contour(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)  # Smaller blur kernel for speed
    edges = cv2.Canny(gray, 50, 150)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3,3))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best_approx = None
    best_area = 0
    for cnt in contours:
        epsilon = 0.02 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)
        if len(approx) == 4:
            area = cv2.contourArea(approx)
            if area > 10000:  # Adjust threshold as needed
                if area > best_area:
                    best_area = area
                    best_approx = approx
    return best_approx

def get_perspective_corrected_card(frame, approx):
    pts = approx.reshape(4, 2)
    pts = sorted(pts, key=lambda x: x[1])  # sort by y
    top_two = pts[:2]
    bottom_two = pts[2:]
    top_left, top_right = sorted(top_two, key=lambda x: x[0])
    bottom_left, bottom_right = sorted(bottom_two, key=lambda x: x[0])

    ordered = np.array([top_left, top_right, bottom_right, bottom_left], dtype="float32")

    dst = np.array([
        [0, 0],
        [WIDTH - 1, 0],
        [WIDTH - 1, HEIGHT - 1],
        [0, HEIGHT - 1]], dtype="float32")

    M = cv2.getPerspectiveTransform(ordered, dst)
    warped = cv2.warpPerspective(frame, M, (WIDTH, HEIGHT))

    # Ensure card is portrait (height > width)
    h, w, _ = warped.shape
    if w > h:
        rotated90 = cv2.rotate(warped, cv2.ROTATE_90_CLOCKWISE)
        rh, rw, _ = rotated90.shape
        if rw > rh:
            rotated180 = cv2.rotate(rotated90, cv2.ROTATE_90_CLOCKWISE)
            rh2, rw2, _ = rotated180.shape
            if rw2 > rh2:
                warped = cv2.rotate(rotated180, cv2.ROTATE_90_CLOCKWISE)
            else:
                warped = rotated180
        else:
            warped = rotated90

    return warped

def extract_card_info(card_id):
    card = card_data_by_id.get(card_id)
    if not card:
        return None

    name = card.get('name', 'Unknown')
    set_code = card.get('set', '???')
    colors = card.get('colors', [])
    color_identity = card.get('color_identity', [])
    cmc = card.get('cmc', None)
    illustration_id = card.get('illustration_id')

    price_str = "null"
    if illustration_id and illustration_id in min_price_by_illustration_id:
        p = min_price_by_illustration_id[illustration_id]
        price_str = f"${p:.2f}"
    else:
        usd_price = card.get('prices', {}).get('usd')
        if usd_price:
            try:
                price_float = float(usd_price)
                price_str = f"${price_float:.2f}"
            except:
                pass

    type_line = card.get('type_line', '').lower()
    possible_types = ["creature", "artifact", "enchantment", "instant", "sorcery", "battle", "planeswalker", "land"]
    found_types = [t for t in possible_types if t in type_line]

    info = {
        "Name": name,
        "Set": set_code,
        "Colors": colors,
        "Color Identity": color_identity,
        "CMC": cmc,
        "Types": found_types,
        "Price": price_str
    }

    return info

def draw_info_as_json(frame, info, start_x=10, start_y=30, line_height=20):
    json_str = json.dumps(info, indent=2)
    lines = json_str.split('\n')
    for i, line in enumerate(lines):
        y = start_y + i * line_height
        cv2.putText(frame, line, (start_x, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

def contours_are_similar(c1, c2, tolerance=0.01):
    x1, y1, w1, h1 = cv2.boundingRect(c1)
    x2, y2, w2, h2 = cv2.boundingRect(c2)
    area_diff = abs((w1 * h1) - (w2 * h2)) / float(w1 * h1 + 1)
    return area_diff < tolerance

# Sorting logic helper functions
def is_basic_land(name):
    basic_lands = ["plains", "island", "swamp", "mountain", "forest", "wastes"]
    return name.lower() in basic_lands

def is_land_card(types):
    return "land" in types

def get_bin_for_color(info):
    if is_land_card(info.get("Types", [])):
        if is_basic_land(info.get("Name", "")):
            return 9  # Basic lands
        else:
            return 8  # Nonbasic lands

    colors = info.get("Colors", [])
    if not colors:
        return 6  # Colorless
    elif len(colors) > 1:
        return 7  # Multicolor
    else:
        c = colors[0]
        if c == "W": return 1
        if c == "U": return 2
        if c == "B": return 3
        if c == "R": return 4
        if c == "G": return 5
    return 6  # Fallback to Colorless

def get_bin_for_mana_value(info):
    mv = info.get("CMC", 0)
    if mv <= 1: return 1
    elif mv == 2: return 2
    elif mv == 3: return 3
    elif mv == 4: return 4
    elif mv == 5: return 5
    elif mv == 6: return 6
    elif mv == 7: return 7
    else: return 8

def get_bin_for_set(info):
    set_code = info.get("Set", "???").lower()
    if set_code == "khm": return 1
    elif set_code == "neo": return 2
    else: return 9

def get_bin_for_price(info):
    price_str = info.get("Price", "null")
    if price_str == "null":
        return 10
    try:
        price = float(price_str.strip('$'))
    except:
        return 10

    if price < 0.5: return 1
    elif price < 1.0: return 2
    elif price < 5.0: return 3
    elif price < 10.0: return 4
    else: return 5

def get_bin_for_type(info):
    types = info.get("Types", [])
    if "creature" in types: return 1
    if "artifact" in types: return 2
    if "enchantment" in types: return 3
    if "instant" in types: return 4
    if "sorcery" in types: return 5
    if "battle" in types: return 6
    if "planeswalker" in types: return 7
    if "land" in types: return 8
    return 9

def get_bin_number(info, mode):
    if not info:
        return 10  # Error bin
    if mode == "color":
        return get_bin_for_color(info)
    elif mode == "mana_value":
        return get_bin_for_mana_value(info)
    elif mode == "set":
        return get_bin_for_set(info)
    elif mode == "price":
        return get_bin_for_price(info)
    elif mode == "type":
        return get_bin_for_type(info)
    else:
        return 10  # Default to Error bin if mode unknown

def print_sorting_options():
    print("Choose your sorting method:")
    print("1 - Color: ")
    print("    White (W): Bin 1")
    print("    Blue (U): Bin 2")
    print("    Black (B): Bin 3")
    print("    Red (R): Bin 4")
    print("    Green (G): Bin 5")
    print("    Colorless: Bin 6")
    print("    Multicolor: Bin 7")
    print("    Nonbasic lands: Bin 8")
    print("    Basic lands: Bin 9")
    print("    Errors: Bin 10")
    print()
    print("2 - CMC: ")
    print("    1: Bin 1")
    print("    2: Bin 2")
    print("    3: Bin 3")
    print("    4: Bin 4")
    print("    5: Bin 5")
    print("    6: Bin 6")
    print("    7: Bin 7")
    print("    8+: Bin 8")
    print("    Errors: Bin 10")
    print()
    print("3 - Set: ")
    print("    KHM: Bin 1")
    print("    NEO: Bin 2")
    print("    Unknown sets: Bin 9")
    print("    Errors: Bin 10")
    print()
    print("4 - Price: ")
    print("    Under $0.5: Bin 1")
    print("    $0.5 to $1: Bin 2")
    print("    $1 to $5: Bin 3")
    print("    $5 to $10: Bin 4")
    print("    Above $10: Bin 5")
    print("    Errors: Bin 10")
    print()
    print("5 - Type:")
    print("    Creature: Bin 1")
    print("    Artifact: Bin 2")
    print("    Enchantment: Bin 3")
    print("    Instant: Bin 4")
    print("    Sorcery: Bin 5")
    print("    Battle: Bin 6")
    print("    Planeswalker: Bin 7")
    print("    Land: Bin 8")
    print("    Unknown: Bin 9")
    print("    Errors: Bin 10")
    print()

def main():
    print_sorting_options()
    choice = input("Enter the number of the sorting method: ").strip()
    mode_map = {
        "1": "color",
        "2": "mana_value",
        "3": "set",
        "4": "price",
        "5": "type"
    }
    current_sorting_mode = mode_map.get(choice, "color")  # Default to color if invalid choice
    print(f"Selected mode: {current_sorting_mode}")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Cannot open webcam.")
        return

    last_contour = None
    last_card_id = None
    last_orientation_rotated = False
    last_warped = None

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Failed to grab frame.")
            break

        # Rotate the frame to portrait mode
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

        # Show live camera view
        cv2.imshow("Webcam (Live View)", frame)

        key = cv2.waitKey(1)  # Check for user input
        if key == 27:  # ESC key
            break
        elif key == 32:  # Spacebar key
            # Run detection only when spacebar is pressed
            display_frame = frame.copy()
            card_approx = find_card_contour(frame)

            if card_approx is not None:
                if last_contour is not None and contours_are_similar(card_approx, last_contour) and last_card_id is not None:
                    # Reuse last result
                    chosen_id = last_card_id
                    chosen_info = extract_card_info(chosen_id)

                    if last_orientation_rotated and last_warped is not None:
                        rotated180 = cv2.rotate(last_warped, cv2.ROTATE_180)
                        cv2.imshow("Card Perspective", rotated180)
                    elif last_warped is not None:
                        cv2.imshow("Card Perspective", last_warped)

                    if chosen_id is None:
                        # Unrecognized
                        cv2.drawContours(display_frame, [card_approx], -1, (0, 0, 255), 2)
                        cv2.putText(display_frame, "Unrecognized Card", (10, 30),
                                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                        bin_number = 10  # Error bin
                        cv2.putText(display_frame, f"Bin: {bin_number}", (10, 200),
                                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                    else:
                        draw_info_as_json(display_frame, chosen_info, start_x=10, start_y=30, line_height=20)
                        bin_number = get_bin_number(chosen_info, current_sorting_mode)
                        cv2.putText(display_frame, f"Bin: {bin_number}", (10, 200),
                                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                    last_card_id = chosen_id
                else:
                    # Need to re-hash
                    cv2.drawContours(display_frame, [card_approx], -1, (0, 255, 0), 2)
                    warped = get_perspective_corrected_card(frame, card_approx)
                    last_warped = warped.copy()

                    cropped_upright = warped[0:CROP_SIZE, 0:CROP_SIZE]
                    img_pil_upright = Image.fromarray(cv2.cvtColor(cropped_upright, cv2.COLOR_BGR2RGB))

                    rotated180 = cv2.rotate(warped, cv2.ROTATE_180)
                    cropped_rotated = rotated180[0:CROP_SIZE, 0:CROP_SIZE]
                    img_pil_rotated = Image.fromarray(cv2.cvtColor(cropped_rotated, cv2.COLOR_BGR2RGB))

                    # Single-threaded hashing for speed with increased hash_size
                    u_comb_id, u_comb_dist = hash_image_color(img_pil_upright, hash_size=16)
                    r_comb_id, r_comb_dist = hash_image_color(img_pil_rotated, hash_size=16)

                    # Collect distances for the top 10 closest matches
                    distances = []
                    img_pil = img_pil_upright if u_comb_dist <= r_comb_dist else img_pil_rotated
                    img_phashes = [imagehash.phash(channel, hash_size=16) for channel in img_pil.split()]
                    for card_id, stored_r, stored_g, stored_b in precomputed_hashes:
                        dist_r = img_phashes[0] - stored_r
                        dist_g = img_phashes[1] - stored_g
                        dist_b = img_phashes[2] - stored_b
                        avg_dist = (dist_r + dist_g + dist_b) / 3.0
                        distances.append((card_id, avg_dist))
                    distances.sort(key=lambda x: x[1])

                    # Ensure there are at least two matches
                    if len(distances) >= 2:
                        top1_id, top1_dist = distances[0]
                        top2_id, top2_dist = distances[1]
                    elif len(distances) == 1:
                        top1_id, top1_dist = distances[0]
                        top2_id, top2_dist = (None, float('inf'))
                    else:
                        top1_id, top1_dist = (None, float('inf'))
                        top2_id, top2_dist = (None, float('inf'))

                    print("Top 10 closest matches:")
                    for rank, (card_id, dist) in enumerate(distances[:10], start=1):
                        card_info = extract_card_info(card_id)
                        card_name = card_info.get("Name", "Unknown") if card_info else "Unknown"
                        print(f"{rank}. Card ID: {card_id}, Name: {card_name}, Distance: {dist:.2f}")

                    # Print the difference between top two matches for debugging
                    if top2_id is not None:
                        print(f"Difference between top two matches: {top2_dist - top1_dist:.2f}")
                    else:
                        print("Only one match found.")

                    # Apply error handling rules
                    error_flag = False
                    chosen_id = top1_id
                    chosen_dist = top1_dist
                    chosen_info = extract_card_info(chosen_id) if chosen_id else None

                    # Retrieve illustration_ids
                    top1_illustration_id = card_data_by_id.get(top1_id, {}).get('illustration_id') if top1_id else None
                    top2_illustration_id = card_data_by_id.get(top2_id, {}).get('illustration_id') if top2_id else None

                    if chosen_dist > MAX_DISTANCE_THRESHOLD:
                        print(f"Error: Best match distance {chosen_dist:.2f} exceeds threshold {MAX_DISTANCE_THRESHOLD}.")
                        error_flag = True
                    elif top2_id and (top2_dist - top1_dist) < 10:
                        # Check if illustration_ids differ
                        if top1_illustration_id != top2_illustration_id:
                            print(f"Error: Difference between top two matches is {top2_dist - top1_dist:.2f}, which is less than 10 and illustration_ids differ.")
                            error_flag = True
                        else:
                            print(f"Ignored ambiguity: Difference between top two matches is {top2_dist - top1_dist:.2f}, but illustration_ids are the same.")
                    # Else, no error

                    if error_flag:
                        cv2.drawContours(display_frame, [card_approx], -1, (0, 0, 255), 2)
                        cv2.putText(display_frame, "Unrecognized Card", (10, 30),
                                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                        bin_number = 10  # Error bin
                        if chosen_info:
                            draw_info_as_json(display_frame, chosen_info, start_x=10, start_y=30, line_height=20)
                        cv2.putText(display_frame, f"Bin: {bin_number}", (10, 200),
                                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                        last_card_id = None
                    else:
                        # Select the best match
                        if r_comb_dist < u_comb_dist:
                            chosen_id = r_comb_id
                            chosen_dist = r_comb_dist
                            chosen_info = extract_card_info(chosen_id)
                            cv2.imshow("Card Perspective", rotated180)
                            last_orientation_rotated = True
                        else:
                            chosen_id = u_comb_id
                            chosen_dist = u_comb_dist
                            chosen_info = extract_card_info(chosen_id)
                            cv2.imshow("Card Perspective", warped)
                            last_orientation_rotated = False

                        # Final error check based on chosen_dist
                        if chosen_id is None or chosen_dist > MAX_DISTANCE_THRESHOLD:
                            cv2.drawContours(display_frame, [card_approx], -1, (0, 0, 255), 2)
                            cv2.putText(display_frame, "Unrecognized Card", (10, 30),
                                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                            bin_number = 10  # Error bin
                            cv2.putText(display_frame, f"Bin: {bin_number}", (10, 200),
                                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                            last_card_id = None
                        else:
                            cv2.drawContours(display_frame, [card_approx], -1, (0, 255, 0), 2)
                            draw_info_as_json(display_frame, chosen_info, start_x=10, start_y=30, line_height=20)
                            bin_number = get_bin_number(chosen_info, current_sorting_mode)
                            cv2.putText(display_frame, f"Bin: {bin_number}", (10, 200),
                                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                            last_card_id = chosen_id

                    last_contour = card_approx
            else:
                blank = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
                cv2.imshow("Card Perspective", blank)
                cv2.putText(display_frame, "No card detected", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                last_contour = None
                last_card_id = None

            # Optionally, display the annotated frame
            cv2.imshow("Detected Card", display_frame)
        # Continue loop

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
