import os
import json
from PIL import Image
import imagehash

def create_top_area_hash_database(images_dir, output_json, crop_size=745):
    """
    Create or update a database of perceptual hashes (phash and dhash) from the top-left crop_size x crop_size region of the card images.
    :param images_dir: Directory where card images (png) are stored.
    :param output_json: Path to store the resulting hash database JSON.
    :param crop_size: Size of the square region to crop (default: 745x745).
    """

    # Load existing hash database if it exists
    if os.path.exists(output_json):
        with open(output_json, 'r', encoding='utf-8') as f:
            hash_db = json.load(f)
    else:
        hash_db = {}

    # Upgrade old format (string hash) to new format (dict with 'phash' and 'dhash')
    # Old format: { "card_id": "hash_str" }
    # New format: { "card_id": {"phash":"...", "dhash":"..."} }
    upgraded = False
    for card_id, value in list(hash_db.items()):
        if isinstance(value, str):
            # This entry only has a phash in old format, convert to new
            hash_db[card_id] = {"phash": value, "dhash": None}
            upgraded = True

    if upgraded:
        print("Upgraded old hash database format to new format with phash/dhash fields.")

    # Get a list of all PNG files in the images_dir
    files = [f for f in os.listdir(images_dir) if f.lower().endswith('.png')]
    total_files = len(files)
    print(f"Found {total_files} PNG files in {images_dir}.")

    processed_count = 0

    for idx, filename in enumerate(files, start=1):
        card_id = os.path.splitext(filename)[0]
        image_path = os.path.join(images_dir, filename)

        # Check if we already have hashes for this card_id
        # If not present, create a new entry
        if card_id not in hash_db:
            hash_db[card_id] = {"phash": None, "dhash": None}

        phash_existing = hash_db[card_id].get('phash')
        dhash_existing = hash_db[card_id].get('dhash')

        # If both phash and dhash exist, skip processing
        if phash_existing is not None and dhash_existing is not None:
            # Uncomment the following line if you want to see skip messages:
            # print(f"Skipping {filename}, both phash and dhash already computed.")
            continue

        print(f"Processing file {idx}/{total_files}: {filename}")

        try:
            with Image.open(image_path) as img:
                img = img.convert('RGB')

                # Crop top-left region
                width, height = img.size
                crop_width = min(crop_size, width)
                crop_height = min(crop_size, height)
                crop_box = (0, 0, crop_width, crop_height)
                cropped_img = img.crop(crop_box)

                # Compute missing hashes
                if phash_existing is None:
                    ph = imagehash.phash(cropped_img)
                    hash_db[card_id]['phash'] = str(ph)
                    print(f"  [SUCCESS] Computed phash for {filename}")

                if dhash_existing is None:
                    dh = imagehash.dhash(cropped_img)
                    hash_db[card_id]['dhash'] = str(dh)
                    print(f"  [SUCCESS] Computed dhash for {filename}")

                processed_count += 1

        except Exception as e:
            print(f"  [ERROR] Failed to process {filename}: {e}")

    # Save the hash database as a JSON file
    try:
        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(hash_db, f, ensure_ascii=False, indent=2)
        print(f"Hash database created/updated at {output_json} with entries for {len(hash_db)} cards. {processed_count} files processed this run.")
    except Exception as e:
        print(f"[ERROR] Failed to write hash database to {output_json}: {e}")

# Example usage:
images_dir = "downloaded_cards"  # directory where your PNG files are stored
output_json = "card_hashes.json"
create_top_area_hash_database(images_dir, output_json, crop_size=745)
