import os
import json
from PIL import Image
import imagehash
from concurrent.futures import ProcessPoolExecutor

# Constants for the art region
ART_X = 77
ART_Y = 107
ART_W = 514
ART_H = 417

def process_image(args):
    filename, images_dir, hash_size = args
    card_id = os.path.splitext(filename)[0]
    image_path = os.path.join(images_dir, filename)

    try:
        with Image.open(image_path) as img:
            img = img.convert('RGB')
            width, height = img.size

            # Ensure the art region fits within the image
            # If the card image is smaller than expected, adjust boundaries
            x1 = min(ART_X, width)
            y1 = min(ART_Y, height)
            x2 = min(ART_X + ART_W, width)
            y2 = min(ART_Y + ART_H, height)

            if x2 <= x1 or y2 <= y1:
                return None, None, f"Art area not valid for {filename}"

            cropped_img = img.crop((x1, y1, x2, y2))

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

def create_color_hash_database(images_dir, output_json, hash_size=16, max_workers=None):
    """
    Create a database of color-based perceptual hashes for the defined art area from card images.
    We compute phash for R, G, and B channels for each image with a given hash_size.
    Logs each processed image to the console.
    """
    files = [f for f in os.listdir(images_dir) if f.lower().endswith('.png')]
    total_files = len(files)
    print(f"Found {total_files} PNG files in {images_dir}.")

    hash_db = {}
    args_list = [(filename, images_dir, hash_size) for filename in files]

    # Use ProcessPoolExecutor for parallel processing
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        for idx, result in enumerate(executor.map(process_image, args_list), start=1):
            card_id, card_hashes, error_msg = result
            if error_msg:
                print(f"[{idx}/{total_files}] ERROR: {error_msg}")
            else:
                hash_db[card_id] = card_hashes
                print(f"[{idx}/{total_files}] Processed {card_id}")

    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(hash_db, f, ensure_ascii=False, indent=2)

    print(f"Hash database created/updated at {output_json} with {len(hash_db)} entries.")

# Example usage:
if __name__ == "__main__":
    images_dir = "downloaded_cards"  # directory where your PNG files are stored
    output_json = "card_hashes_smaller.json"
    create_color_hash_database(images_dir, output_json, hash_size=16, max_workers=None)
