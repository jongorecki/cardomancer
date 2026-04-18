import os
import json
from PIL import Image
import imagehash
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

# Constants
WIDTH = 745
HEIGHT = 1040

# Stable regions in the full image to analyze for signatures.
# (y_start, y_end, x_start, x_end) - these areas should remain stable for cards sharing the same frame/frame_effects.
STABLE_REGIONS = {
    "top_border":    (0, 50, 0, WIDTH),
    "left_border":   (0, HEIGHT, 0, 50),
    "bottom_border": (HEIGHT - 50, HEIGHT, 0, WIDTH)
}

# Paths (adjust as needed)
cards_json_path = r"C:\Users\Jon\Downloads\default-cards-20241206100658.json"
images_dir = "downloaded_cards"   
output_json = "frame_signatures.json"

def load_card_data(json_path):
    if os.path.exists(json_path):
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def get_frame_effect_tuple(card):
    frame_effects = card.get('frame_effects', [])
    if frame_effects is None:
        frame_effects = []
    frame_effects = sorted(frame_effects)
    return tuple(frame_effects)

def compute_region_phash(img_pil, region):
    y_start, y_end, x_start, x_end = region
    region_img = img_pil.crop((x_start, y_start, x_end, y_end))
    return imagehash.phash(region_img)

def process_card_image(args):
    """
    Process a single card image to compute the stable region phashes.
    Returns (card_id, frame, frame_effects_tuple, {region_name: pHash_str or None}, status_msg)
    """
    card, images_dir = args
    card_id = card.get('id')
    frame = card.get('frame')
    frame_effects_tuple = get_frame_effect_tuple(card)

    # If frame is None, skip this card.
    if frame is None:
        return card_id, None, None, None, f"[SKIP] Card {card_id}: No frame attribute."

    image_filename = f"{card_id}.png"
    image_path = os.path.join(images_dir, image_filename)

    if not os.path.exists(image_path):
        return card_id, frame, frame_effects_tuple, None, f"[FAIL] Card {card_id}: Image not found at {image_path}"

    try:
        img = Image.open(image_path).convert('RGB')
    except Exception as e:
        return card_id, frame, frame_effects_tuple, None, f"[FAIL] Card {card_id}: Failed to open image: {e}"

    if img.size != (WIDTH, HEIGHT):
        return (card_id, frame, frame_effects_tuple, None,
                f"[FAIL] Card {card_id}: Image size {img.size} != {WIDTH}x{HEIGHT}")

    region_phashes = {}
    for region_name, region_coords in STABLE_REGIONS.items():
        try:
            ph = compute_region_phash(img, region_coords)
            region_phashes[region_name] = str(ph)
        except Exception as e:
            return card_id, frame, frame_effects_tuple, None, f"[FAIL] Card {card_id}: pHash for {region_name} failed: {e}"

    # If we got here, success
    return card_id, frame, frame_effects_tuple, region_phashes, f"[SUCCESS] Card {card_id} processed."

def main():
    cards_data = load_card_data(cards_json_path)
    total_cards = len(cards_data)
    print(f"Loaded {total_cards} cards from JSON.")

    # We'll group cards by (frame, frame_effects_tuple)
    # First, filter cards that have a frame
    filtered_cards = [c for c in cards_data if c.get('frame') is not None]

    # Use a process pool to parallelize
    args_list = [(card, images_dir) for card in filtered_cards]

    signatures = defaultdict(lambda: {"top_border": [], "left_border": [], "bottom_border": []})
    processed_count = 0

    with ProcessPoolExecutor() as executor:
        for card_id, frame, fe_tuple, region_phashes, msg in executor.map(process_card_image, args_list):
            print(msg)
            if region_phashes is None or frame is None or fe_tuple is None:
                # Means fail or skip
                continue

            # Append these phashes to the combination
            combo_key = (frame, fe_tuple)
            for region_name, ph in region_phashes.items():
                signatures[combo_key][region_name].append(ph)
            processed_count += 1

    # Convert defaultdict to normal dict
    signatures_dict = {}
    for combo_key, regions_dict in signatures.items():
        frame, fe_tuple = combo_key
        fe_list = list(fe_tuple)
        fe_list_str = ",".join(fe_list)
        if frame not in signatures_dict:
            signatures_dict[frame] = {}
        signatures_dict[frame][fe_list_str] = regions_dict

    # Save to JSON
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(signatures_dict, f, ensure_ascii=False, indent=2)

    print(f"\nFrame/Frame_effects signatures saved to {output_json}")
    print(f"Processed {processed_count}/{total_cards} cards successfully.")

if __name__ == "__main__":
    main()
