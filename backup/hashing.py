# hashing.py
# Handles the hashing and distance calculation logic.
# Uses precomputed hash database from card_hashes.json and imagehash library.

import os
import json
from PIL import Image
import imagehash
from config import HASH_DB_PATH

# Load hash database
if os.path.exists(HASH_DB_PATH):
    with open(HASH_DB_PATH, 'r', encoding='utf-8') as f:
        HASH_DB = json.load(f)
else:
    HASH_DB = {}

# Precompute hash objects for faster lookup
PRECOMPUTED_HASHES = []
for card_id, h in HASH_DB.items():
    r_phash_str = h.get('r_phash')
    g_phash_str = h.get('g_phash')
    b_phash_str = h.get('b_phash')
    if r_phash_str and g_phash_str and b_phash_str:
        try:
            stored_r = imagehash.hex_to_hash(r_phash_str)
            stored_g = imagehash.hex_to_hash(g_phash_str)
            stored_b = imagehash.hex_to_hash(b_phash_str)
            PRECOMPUTED_HASHES.append((card_id, stored_r, stored_g, stored_b))
        except ValueError:
            print(f"Invalid hash format for card {card_id}. Skipping.")


def hash_image_color(img_pil, hash_size=16):
    """
    Compute the perceptual hash (phash) for the R, G, and B channels of the given PIL image.
    Returns (best_id, best_distance) for the closest match in PRECOMPUTED_HASHES.
    """
    img_pil = img_pil.convert('RGB')
    r, g, b = img_pil.split()

    r_ph = imagehash.phash(r, hash_size=hash_size)
    g_ph = imagehash.phash(g, hash_size=hash_size)
    b_ph = imagehash.phash(b, hash_size=hash_size)

    best_id = None
    best_dist = float('inf')

    for (card_id, stored_r, stored_g, stored_b) in PRECOMPUTED_HASHES:
        dist_r = r_ph - stored_r
        dist_g = g_ph - stored_g
        dist_b = b_ph - stored_b
        dist = (dist_r + dist_g + dist_b) / 3.0
        if dist < best_dist:
            best_dist = dist
            best_id = card_id

    return best_id, best_dist


def compute_distances_for_image(img_pil, hash_size=16):
    """
    Compute phashes for the given PIL image and return a list of (card_id, distance) for all cards.
    This allows us to filter and sort matches in the main logic.
    """
    img_pil = img_pil.convert('RGB')
    r, g, b = img_pil.split()

    r_ph = imagehash.phash(r, hash_size=hash_size)
    g_ph = imagehash.phash(g, hash_size=hash_size)
    b_ph = imagehash.phash(b, hash_size=hash_size)

    results = []
    for (card_id, stored_r, stored_g, stored_b) in PRECOMPUTED_HASHES:
        dist_r = r_ph - stored_r
        dist_g = g_ph - stored_g
        dist_b = b_ph - stored_b
        avg_dist = (dist_r + dist_g + dist_b) / 3.0
        results.append((card_id, avg_dist))
    return results
