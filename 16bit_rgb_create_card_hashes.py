import os
import json
import cv2
import numpy as np
from PIL import Image
import imagehash
from concurrent.futures import ProcessPoolExecutor

# CLAHE settings — must match detection.py color_correct_card()
CLAHE_CLIP_LIMIT = 2.0
CLAHE_GRID_SIZE = 8


def apply_clahe(pil_img):
    """
    Apply CLAHE normalization to a PIL RGB image.
    Matches the same CLAHE applied to camera-captured images in
    detection.py color_correct_card(), so both sides of the hash
    comparison are normalized identically.
    """
    img_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)

    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT,
                             tileGridSize=(CLAHE_GRID_SIZE, CLAHE_GRID_SIZE))
    l_corrected = clahe.apply(l_ch)

    corrected_lab = cv2.merge([l_corrected, a_ch, b_ch])
    corrected_bgr = cv2.cvtColor(corrected_lab, cv2.COLOR_LAB2BGR)
    corrected_rgb = cv2.cvtColor(corrected_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(corrected_rgb)


def process_image(args):
    filename, images_dir, crop_size, hash_size = args
    card_id = os.path.splitext(filename)[0]
    image_path = os.path.join(images_dir, filename)

    try:
        with Image.open(image_path) as img:
            img = img.convert('RGB')
            width, height = img.size
            crop_width = min(crop_size, width)
            crop_height = min(crop_size, height)
            crop_box = (0, 0, crop_width, crop_height)
            cropped_img = img.crop(crop_box)

            # Apply CLAHE normalization before hashing
            cropped_img = apply_clahe(cropped_img)

            r, g, b = cropped_img.split()
            r_ph = imagehash.phash(r, hash_size=hash_size)
            g_ph = imagehash.phash(g, hash_size=hash_size)
            b_ph = imagehash.phash(b, hash_size=hash_size)

            return card_id, {
                "r_phash": str(r_ph),
                "g_phash": str(g_ph),
                "b_phash": str(b_ph)
            }, None
    except Exception as e:
        return None, None, f"Failed to process {filename}: {e}"

def create_color_hash_database(images_dir, output_json, crop_size=745, hash_size=16, max_workers=None):
    """
    Create a database of color-based perceptual hashes from card images using parallel processing.
    We compute phash for R, G, and B channels for each image with increased hash resolution.
    Logs each processed image to the console.
    """
    files = [f for f in os.listdir(images_dir) if f.lower().endswith('.png')]
    total_files = len(files)
    print(f"Found {total_files} PNG files in {images_dir}.")

    hash_db = {}

    # Prepare arguments for each image, including the new hash_size
    args_list = [(filename, images_dir, crop_size, hash_size) for filename in files]

    # Use ProcessPoolExecutor for parallel processing
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        for idx, result in enumerate(executor.map(process_image, args_list), start=1):
            card_id, card_hashes, error_msg = result
            if error_msg:
                print(f"[{idx}/{total_files}] ERROR: {error_msg}")
            else:
                # Success
                hash_db[card_id] = card_hashes
                print(f"[{idx}/{total_files}] Processed {card_id}")

    # Save the hash database as a JSON file
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(hash_db, f, ensure_ascii=False, indent=2)

    print(f"Hash database created/updated at {output_json} with {len(hash_db)} entries.")

# Example usage:
if __name__ == "__main__":
    images_dir = "downloaded_cards"  # directory where your PNG files are stored
    output_json = "card_hashes.json"
    create_color_hash_database(images_dir, output_json, crop_size=745, hash_size=16, max_workers=None)
