# build_hash_db_v3.py
# ---------------------------------------------------------------------------
# Build the v3 hash database: 3 fixed regions, per-channel RGB phash.
#
# Regions (on 745x1040 Scryfall PNGs):
#   A: (30, 105, 715, 520)  — standard art area, full width
#   B: (30,  60, 350, 580)  — left strip (class/case art)
#   C: (395,  60, 715, 580) — right strip (saga art)
#
# Each region gets per-channel (R, G, B) phash with hash_size=16
# (256-bit hashes). No CLAHE normalization.
#
# Usage:
#   python build_hash_db_v3.py
#
# Output: card_hashes_v3.json
# ---------------------------------------------------------------------------

import os
import json
import time
from PIL import Image
import imagehash
from concurrent.futures import ProcessPoolExecutor

# --- Region definitions (x1, y1, x2, y2) on 745x1040 images ---
REGION_A = (30, 105, 715, 520)   # Standard art — full width
REGION_B = (30,  60, 350, 580)   # Left strip — class/case
REGION_C = (395,  60, 715, 580)  # Right strip — saga

# Back-face filename suffix (matches download_cards.py)
BACK_FACE_SUFFIX = '__back'

# Card back reference
CARD_BACK_REF = 'card_back_reference.png'


HASH_SIZE = 16  # 16x16 = 256-bit hashes


def _hash_region(img_rgb, region):
    """Crop a region from a PIL RGB image and compute per-channel phash + dhash."""
    x1, y1, x2, y2 = region
    w, h = img_rgb.size
    crop = img_rgb.crop((x1, y1, min(x2, w), min(y2, h)))
    r, g, b = crop.split()
    return (
        # phash (frequency domain)
        str(imagehash.phash(r, hash_size=HASH_SIZE)),
        str(imagehash.phash(g, hash_size=HASH_SIZE)),
        str(imagehash.phash(b, hash_size=HASH_SIZE)),
        # dhash (gradient domain)
        str(imagehash.dhash(r, hash_size=HASH_SIZE)),
        str(imagehash.dhash(g, hash_size=HASH_SIZE)),
        str(imagehash.dhash(b, hash_size=HASH_SIZE)),
    )


def process_image(args):
    """Process a single card image: hash all 3 regions."""
    filename, images_dir = args
    file_stem = os.path.splitext(filename)[0]
    image_path = os.path.join(images_dir, filename)

    # Detect back-face
    if file_stem.endswith(BACK_FACE_SUFFIX):
        canonical_id = file_stem[:-len(BACK_FACE_SUFFIX)]
    else:
        canonical_id = None

    try:
        with Image.open(image_path) as img:
            img = img.convert('RGB')

            # Hash all 3 regions (each returns 6 hashes: 3 phash + 3 dhash)
            a_pr, a_pg, a_pb, a_dr, a_dg, a_db = _hash_region(img, REGION_A)
            b_pr, b_pg, b_pb, b_dr, b_dg, b_db = _hash_region(img, REGION_B)
            c_pr, c_pg, c_pb, c_dr, c_dg, c_db = _hash_region(img, REGION_C)

            entry = {
                'a_pr': a_pr, 'a_pg': a_pg, 'a_pb': a_pb,
                'a_dr': a_dr, 'a_dg': a_dg, 'a_db': a_db,
                'b_pr': b_pr, 'b_pg': b_pg, 'b_pb': b_pb,
                'b_dr': b_dr, 'b_dg': b_dg, 'b_db': b_db,
                'c_pr': c_pr, 'c_pg': c_pg, 'c_pb': c_pb,
                'c_dr': c_dr, 'c_dg': c_dg, 'c_db': c_db,
            }

            if canonical_id is not None:
                entry['canonical_id'] = canonical_id

            return file_stem, entry, None

    except Exception as e:
        return None, None, f"Failed to process {filename}: {e}"


def hash_card_back(script_dir):
    """Hash the card back reference image (Region A only)."""
    ref_path = os.path.join(script_dir, CARD_BACK_REF)
    if not os.path.exists(ref_path):
        print(f"WARNING: Card back reference not found at {ref_path}")
        return None

    with Image.open(ref_path) as img:
        img = img.convert('RGB')
        # Crop to 745x1040 if needed (reference is 745x1043)
        w, h = img.size
        if h > 1040:
            img = img.crop((0, 0, w, 1040))

        a_pr, a_pg, a_pb, a_dr, a_dg, a_db = _hash_region(img, REGION_A)
        return {
            'a_pr': a_pr, 'a_pg': a_pg, 'a_pb': a_pb,
            'a_dr': a_dr, 'a_dg': a_dg, 'a_db': a_db,
        }


# --- Non-gameplay card exclusion ---
# These layouts / card types produce false positives and should never be in
# the hash DB. Tokens and emblems are intentionally KEPT (we sort those).
EXCLUDED_LAYOUTS = {'art_series', 'planar', 'scheme', 'vanguard'}

def should_exclude_card(card):
    """Return True if this card should NOT be in the hash DB."""
    if card.get('oversized', False):
        return True
    if card.get('layout', '') in EXCLUDED_LAYOUTS:
        return True
    type_line = card.get('type_line', '')
    name = card.get('name', '')
    set_type = card.get('set_type', '')
    # Bio cards, checklist/substitute cards, other non-game memorabilia
    if type_line == 'Card' and set_type == 'memorabilia':
        return True
    if 'Substitute' in name and type_line == 'Card':
        return True
    if set_type == 'minigame':
        return True
    return False


def _build_exclusion_set(script_dir):
    """Load card data and return set of card IDs to exclude."""
    try:
        from config import CARDS_JSON_PATH
        import json as _json
        with open(CARDS_JSON_PATH, 'r', encoding='utf-8') as f:
            all_cards = _json.load(f)
        excluded = set()
        for c in all_cards:
            if should_exclude_card(c):
                excluded.add(c.get('id', ''))
        return excluded
    except Exception as e:
        print(f"WARNING: Could not load card data for exclusion filter: {e}")
        return set()


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    images_dir = os.path.join(script_dir, 'downloaded_cards')
    output_json = os.path.join(script_dir, 'card_hashes_v3.json')

    if not os.path.isdir(images_dir):
        print(f"ERROR: Directory '{images_dir}' not found.")
        return

    # Build exclusion set from card data
    print("Loading card data for exclusion filter...")
    excluded_ids = _build_exclusion_set(script_dir)
    print(f"  {len(excluded_ids)} non-gameplay cards to exclude "
          f"(oversized, art series, bios, schemes, etc.)")

    all_files = [f for f in os.listdir(images_dir) if f.lower().endswith('.png')]

    # Filter out excluded cards
    files = []
    skipped = 0
    for f in all_files:
        stem = os.path.splitext(f)[0]
        # Strip __back suffix to get canonical ID
        canon = stem[:-len(BACK_FACE_SUFFIX)] if stem.endswith(BACK_FACE_SUFFIX) else stem
        if canon in excluded_ids:
            skipped += 1
        else:
            files.append(f)

    total = len(files)
    print(f"Found {len(all_files)} PNG files, excluded {skipped}, hashing {total}")
    print(f"Regions: A={REGION_A}, B={REGION_B}, C={REGION_C}")
    print(f"Hash: per-channel RGB phash, hash_size={HASH_SIZE} ({HASH_SIZE**2}-bit)")
    print()

    args_list = [(f, images_dir) for f in files]
    hash_db = {}
    errors = 0
    start_time = time.time()

    with ProcessPoolExecutor() as executor:
        for idx, result in enumerate(executor.map(process_image, args_list), start=1):
            card_id, card_hashes, error_msg = result
            if error_msg:
                errors += 1
                if errors <= 20:
                    print(f"  ERROR: {error_msg}")
            else:
                hash_db[card_id] = card_hashes

            if idx % 1000 == 0 or idx == total:
                elapsed = time.time() - start_time
                rate = idx / elapsed
                eta = (total - idx) / rate if rate > 0 else 0
                print(f"  [{idx:>6}/{total}] {idx/total*100:5.1f}%  "
                      f"{rate:>5.0f} cards/sec  ETA {eta:>4.0f}s  "
                      f"({errors} errors)", flush=True)

    # Hash card back reference
    print("Hashing card back reference...")
    card_back = hash_card_back(script_dir)
    if card_back:
        hash_db['_card_back'] = card_back
        print("  Card back reference hashed")

    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s ({total/elapsed:.0f} cards/sec)")
    print(f"  Processed: {len(hash_db)}")
    print(f"  Errors: {errors}")

    print(f"Saving to {output_json}...")
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(hash_db, f, ensure_ascii=False)

    size_mb = os.path.getsize(output_json) / (1024 * 1024)
    print(f"Saved {output_json} ({size_mb:.1f} MB, {len(hash_db)} entries)")


if __name__ == "__main__":
    main()
