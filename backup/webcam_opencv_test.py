import cv2
import numpy as np
import json
from PIL import Image
import imagehash
import os

# Constants
MAX_DISTANCE_THRESHOLD = 30
CROP_SIZE = 745

# Sorting modes definition (mode name -> number)
sorting_modes = {
    1: "color",
    2: "mana_value",
    3: "set",
    4: "price",
    5: "type"
}

# Instructions for each mode (for user prompt)
sorting_instructions = {
    "color": """Color Sorting:
Bin Assignments:
1: White
2: Blue
3: Black
4: Red
5: Green
6: Colorless
7: Multicolor
8: Nonbasic Lands
9: Basic Lands
10: Errors""",
    "mana_value": """CMC Sorting:
Bin Assignments:
1: MV=1
2: MV=2
3: MV=3
4: MV=4
5: MV=5
6: MV=6
7: MV=7
8: MV≥8
10: Errors""",
    "set": """Set Sorting:
(Example: Specific sets to bins)
Bin Assignments:
1: KHM
2: NEO
9: Unknown Set
10: Errors""",
    "price": """Price Sorting:
Bin Assignments:
1: < $0.50
2: $0.50 to $1
3: $1 to $5
4: $5 to $10
5: > $10
10: Errors""",
    "type": """Type Sorting:
Bin Assignments:
1: Creature
2: Artifact
3: Enchantment
4: Instant
5: Sorcery
6: Battle
7: Planeswalker
8: Land
9: Other
10: Errors"""
}

# Prompt the user
def choose_sort_mode():
    print("Choose your sorting method:")
    print("1 - Color (White, Blue, Black, Red, Green, Colorless, Multicolor, Nonbasic lands, Basic lands)")
    print("2 - CMC (1, 2, 3, 4, 5, 6, 7, 8+)")
    print("3 - Set")
    print("4 - Price")
    print("5 - Type")

    choice = input("Enter the number of your choice: ")
    try:
        choice_num = int(choice)
        if choice_num in sorting_modes:
            mode = sorting_modes[choice_num]
            print(sorting_instructions[mode])
            input("Press Enter to start with this sorting mode...")
            return mode
        else:
            print("Invalid choice. Defaulting to 'color' mode.")
            return "color"
    except:
        print("Invalid input. Defaulting to 'color' mode.")
        return "color"


hash_db_path = "card_hashes.json"
cards_json_path = r"C:\Users\Jon\Downloads\default-cards-20241206100658.json"

if os.path.exists(hash_db_path):
    with open(hash_db_path, 'r', encoding='utf-8') as f:
        hash_db = json.load(f)
else:
    hash_db = {}

if os.path.exists(cards_json_path):
    with open(cards_json_path, 'r', encoding='utf-8') as f:
        cards_data = json.load(f)
    card_data_by_id = {c.get('id'): c for c in cards_data}
else:
    cards_data = []
    card_data_by_id = {}

min_price_by_illustration_id = {}
for c in cards_data:
    illustration_id = c.get('illustration_id')
    if not illustration_id:
        continue
    usd_price = c.get('prices', {}).get('usd')
    if usd_price:
        try:
            p = float(usd_price)
            if illustration_id not in min_price_by_illustration_id or p < min_price_by_illustration_id[illustration_id]:
                min_price_by_illustration_id[illustration_id] = p
        except:
            pass

def find_card_contour(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
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
            if area > 10000:
                if area > best_area:
                    best_area = area
                    best_approx = approx
    return best_approx

def order_points(pts):
    pts = sorted(pts, key=lambda x: x[1])  # sort by y
    top_two = pts[:2]
    bottom_two = pts[2:]
    top_left, top_right = sorted(top_two, key=lambda x: x[0])
    bottom_left, bottom_right = sorted(bottom_two, key=lambda x: x[0])
    return np.array([top_left, top_right, bottom_right, bottom_left], dtype="float32")

def get_perspective_corrected_card(frame, approx):
    width = 745
    height = 1043

    pts = approx.reshape(4, 2)
    ordered = order_points(pts)

    dst = np.array([
        [0, 0],
        [width - 1, 0],
        [width - 1, height - 1],
        [0, height - 1]], dtype="float32")

    M = cv2.getPerspectiveTransform(ordered, dst)
    warped = cv2.warpPerspective(frame, M, (width, height))

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

def pil_image_from_opencv(cv_img):
    cv_img_rgb = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
    return Image.fromarray(cv_img_rgb)

def find_best_matches(img_pil):
    captured_ph = imagehash.phash(img_pil)
    captured_dh = imagehash.dhash(img_pil)

    best_combined_id = None
    best_combined_dist = float('inf')

    for card_id, hashes in hash_db.items():
        stored_phash_str = hashes.get('phash')
        stored_dhash_str = hashes.get('dhash')
        if stored_phash_str is None or stored_dhash_str is None:
            continue

        stored_phash = imagehash.hex_to_hash(stored_phash_str)
        stored_dhash = imagehash.hex_to_hash(stored_dhash_str)

        dist_ph = captured_ph - stored_phash
        dist_dh = captured_dh - stored_dhash
        combined_dist = dist_ph + dist_dh

        if combined_dist < best_combined_dist:
            best_combined_dist = combined_dist
            best_combined_id = card_id

    return best_combined_id, best_combined_dist

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
    area_diff = abs((w1*h1) - (w2*h2)) / float(w1*h1 + 1)
    return area_diff < tolerance

# Sorting logic helper functions
def is_basic_land(name):
    # Basic lands: "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"
    basic_lands = ["plains", "island", "swamp", "mountain", "forest", "wastes"]
    return name.lower() in basic_lands

def is_land_card(types):
    return "land" in types

def get_bin_for_color(info):
    # Use Colors, not Color Identity
    # If it's a land, decide if basic or nonbasic:
    if is_land_card(info.get("Types", [])):
        if is_basic_land(info.get("Name", "")):
            return 9  # Basic lands
        else:
            return 8  # Nonbasic lands

    colors = info.get("Colors", [])
    if not colors:
        # Colorless
        return 6
    elif len(colors) > 1:
        # Multicolor
        return 7
    else:
        c = colors[0]
        if c == "W": return 1
        if c == "U": return 2
        if c == "B": return 3
        if c == "R": return 4
        if c == "G": return 5
    return 6  # fallback

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
    # Example logic
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
        return 10  # error
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
        return 10  # default error bin if mode unknown

def main():
    current_sorting_mode = choose_sort_mode()

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

        display_frame = frame.copy()
        card_approx = find_card_contour(frame)

        if card_approx is not None:
            if last_contour is not None and contours_are_similar(card_approx, last_contour) and last_card_id is not None:
                chosen_id = last_card_id
                chosen_info = extract_card_info(chosen_id)

                if last_orientation_rotated and last_warped is not None:
                    rotated180 = cv2.rotate(last_warped, cv2.ROTATE_180)
                    cv2.imshow("Card Perspective", rotated180)
                elif last_warped is not None:
                    cv2.imshow("Card Perspective", last_warped)

                if chosen_id is None:
                    cv2.drawContours(display_frame, [card_approx], -1, (0, 0, 255), 2)
                    cv2.putText(display_frame, "Unrecognized Card", (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                    bin_number = 10
                else:
                    draw_info_as_json(display_frame, chosen_info, start_x=10, start_y=30, line_height=20)
                    bin_number = get_bin_number(chosen_info, current_sorting_mode)
                    cv2.putText(display_frame, f"Bin: {bin_number}", (10, 200),
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            else:
                cv2.drawContours(display_frame, [card_approx], -1, (0, 255, 0), 2)
                warped = get_perspective_corrected_card(frame, card_approx)
                last_warped = warped.copy()

                cropped_upright = warped[0:CROP_SIZE, 0:CROP_SIZE]
                img_pil_upright = pil_image_from_opencv(cropped_upright)
                u_comb_id, u_comb_dist = find_best_matches(img_pil_upright)

                rotated180 = cv2.rotate(warped, cv2.ROTATE_180)
                cropped_rotated = rotated180[0:CROP_SIZE, 0:CROP_SIZE]
                img_pil_rotated = pil_image_from_opencv(cropped_rotated)
                r_comb_id, r_comb_dist = find_best_matches(img_pil_rotated)

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

                if chosen_id is None or chosen_dist > MAX_DISTANCE_THRESHOLD:
                    cv2.drawContours(display_frame, [card_approx], -1, (0, 0, 255), 2)
                    cv2.putText(display_frame, "Unrecognized Card", (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                    last_card_id = None
                    bin_number = 10
                else:
                    draw_info_as_json(display_frame, chosen_info, start_x=10, start_y=30, line_height=20)
                    bin_number = get_bin_number(chosen_info, current_sorting_mode)
                    cv2.putText(display_frame, f"Bin: {bin_number}", (10, 200),
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                    last_card_id = chosen_id

                last_contour = card_approx

        else:
            blank = np.zeros((1043, 745, 3), dtype=np.uint8)
            cv2.imshow("Card Perspective", blank)
            cv2.putText(display_frame, "No card detected", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
            last_contour = None
            last_card_id = None

        cv2.imshow("Webcam (Card Detection)", display_frame)

        key = cv2.waitKey(10)
        if key == 27:  # ESC key
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
