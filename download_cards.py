import json
import os
import glob
import requests
import time


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def download_scryfall_bulk_data():
    """
    Download the latest Scryfall 'default_cards' bulk data JSON.
    Checks the Scryfall bulk-data API for the download URI,
    skips if a file with the same name already exists.
    Returns the path to the JSON file.
    """
    print("[bulk] Fetching Scryfall bulk data catalog...")
    resp = requests.get("https://api.scryfall.com/bulk-data")
    resp.raise_for_status()
    catalog = resp.json()

    # Find the 'default_cards' entry
    default_entry = None
    for entry in catalog.get('data', []):
        if entry.get('type') == 'default_cards':
            default_entry = entry
            break

    if not default_entry:
        print("[bulk] ERROR: Could not find 'default_cards' in Scryfall bulk data catalog.")
        return None

    download_uri = default_entry['download_uri']
    # Extract filename from URI (e.g., "default-cards-20260315100000.json")
    filename = download_uri.split('/')[-1].split('?')[0]
    output_path = os.path.join(SCRIPT_DIR, filename)

    if os.path.exists(output_path):
        print(f"[bulk] Already have {filename}, skipping download.")
        return output_path

    print(f"[bulk] Downloading {filename} ...")
    print(f"[bulk] URI: {download_uri}")
    resp = requests.get(download_uri, stream=True)
    resp.raise_for_status()

    # Stream to file (these are large, ~200MB+)
    total = int(resp.headers.get('content-length', 0))
    downloaded = 0
    with open(output_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded / total * 100
                print(f"\r[bulk] {downloaded / 1024 / 1024:.1f} MB / "
                      f"{total / 1024 / 1024:.1f} MB ({pct:.0f}%)", end='')
    print(f"\n[bulk] Saved to {output_path}")
    return output_path


# Multi-face layouts where BOTH faces are real gameplay cards we want imaged.
# The user can drop a double-faced card on the staging platform with either
# side facing up, so both hashes must exist in the DB.
#
# Everything else (art_series, double_faced_token, etc.) is left alone — if
# it doesn't have a top-level image_uris, it stays skipped like before.
_LIFTABLE_LAYOUTS = {
    'transform',       # werewolves, battles, Innistrad DFCs
    'modal_dfc',       # Zendikar Rising pathways, etc.
    'reversible_card', # The List reversible reprints
    'meld',            # Meld front halves (back is the melded form)
}

# Synthetic suffix appended to the real card_id for the back face. The
# download pipeline saves face 1 under `{id}{_BACK_FACE_SUFFIX}.png`, the
# hash builder keys the entry by the same stem, and `hashing.py` writes
# the real card_id into the precomputed tuple so matches transparently
# resolve back to the canonical card.
_BACK_FACE_SUFFIX = '__back'


def _is_back_face_id(card_id):
    return isinstance(card_id, str) and card_id.endswith(_BACK_FACE_SUFFIX)


def _lift_face_variants(card):
    """
    Return a list of 'lifted' card dicts (0, 1, or 2 items) that should be
    pushed through the download / dedup pipeline.

    - Cards with a top-level image_uris: returns [card] unchanged.
    - Multi-face gameplay cards (transform/modal_dfc/reversible/meld):
      returns up to two shallow-copied entries — one for face 0 (keeps the
      real card_id and real illustration_id) and one for face 1 with a
      synthetic id `{real_id}__back`. The synthetic id gives the back face
      its own filename on disk and its own key in the hash DB, while
      downstream match resolution maps it back to the real card via the
      hash DB's `canonical_id` field.
    - Anything else with no top-level image_uris: returns []. Same as the
      old behavior — silently skipped.
    """
    # Normal single-face path: pass through.
    if 'image_uris' in card:
        return [card]

    if card.get('layout') not in _LIFTABLE_LAYOUTS:
        return []

    faces = card.get('card_faces') or []
    if not faces:
        return []

    real_id = card.get('id')
    variants = []

    # --- Face 0 (front) — keeps the real id ---
    f0 = faces[0]
    f0_imgs = f0.get('image_uris')
    if f0_imgs:
        front = dict(card)
        front['image_uris'] = f0_imgs
        if not front.get('illustration_id'):
            fi = f0.get('illustration_id')
            if fi:
                front['illustration_id'] = fi
        variants.append(front)

    # --- Face 1 (back) — synthetic id so it gets a distinct filename ---
    if real_id and len(faces) > 1:
        f1 = faces[1]
        f1_imgs = f1.get('image_uris')
        if f1_imgs:
            back = dict(card)
            back['image_uris'] = f1_imgs
            back['id'] = f"{real_id}{_BACK_FACE_SUFFIX}"
            # Use face 1's own illustration_id for dedup — different
            # printings of the same DFC share the same back-face artwork.
            back['illustration_id'] = f1.get('illustration_id')
            # Preserve a pointer to the real card id so downstream stages
            # can resolve back to the canonical card. Not used by the
            # downloader directly, but handy for printings_map filtering
            # and for anyone reading the intermediate object.
            back['_canonical_card_id'] = real_id
            variants.append(back)

    return variants


def build_printings_map(all_cards):
    """
    Group cards by (illustration_id, frame). For each unique (art, frame)
    combination, pick one representative card (preferring English, paper,
    non-excluded sets) and record all printings sharing the same art.

    Why (illustration_id, frame) instead of illustration_id alone?
    Phash Region A (30, 105, 715, 520) spans title + mana cost + top border
    — not just the art. A reprint with a different frame (e.g. Zombie
    Infestation: 1997, 2003, 2015) produces a different phash from the same
    art because the title-box / mana-cost rendering / border style differ.
    Before this change, only one frame variant per art was downloaded, and
    scans of the other frames matched at distance 100+ (no match). Now each
    (art, frame) combination gets its own reference image in the hash DB.

    Transform / modal_dfc / battle / reversible cards are handled by lifting
    `card_faces[0].image_uris` and `card_faces[0].illustration_id` up to the
    top level so they flow through the dedup pipeline like any other card.

    Returns:
        representative_cards: list of card dicts to download images for
            (one per (illustration_id, frame) combination)
        printings_map: { representative_card_id: { "name": ..., "printings": [...] } }
            The `printings` list contains ALL cards sharing the
            illustration_id across all frames — downstream UX shows every
            possible printing, not just the same-frame ones.
    """
    from config import EXCLUDED_SETS

    # Group by (illustration_id, frame). Missing/empty frame stays in its
    # own bucket (``''``) so old/unusual entries don't merge with real frame
    # versions.
    by_illust_frame = {}
    # Parallel index: illustration_id → list of all variants sharing that art,
    # across all frames. Used to build each representative's printings list.
    by_illust = {}
    no_illustration = []

    lifted_front = 0
    lifted_back = 0

    for card in all_cards:
        # A single card may expand into two download items (front + back).
        # Single-face normal cards come out as [card] unchanged.
        variants = _lift_face_variants(card)
        if not variants:
            continue

        card_had_top_image = 'image_uris' in card
        for v in variants:
            if _is_back_face_id(v.get('id')):
                lifted_back += 1
            elif not card_had_top_image:
                lifted_front += 1

            illust_id = v.get('illustration_id')
            if not illust_id or 'image_uris' not in v:
                no_illustration.append(v)
                continue

            frame = v.get('frame') or ''
            key = (illust_id, frame)
            by_illust_frame.setdefault(key, []).append(v)
            by_illust.setdefault(illust_id, []).append(v)

    print(f"[dedup] lifted {lifted_front} front-face + {lifted_back} back-face "
          f"images (transform/mdfc/reversible/meld)")

    n_illustrations = len(by_illust)
    n_illust_frames = len(by_illust_frame)
    print(f"[dedup] {n_illustrations} unique illustrations, "
          f"{n_illust_frames} (illustration, frame) combinations, "
          f"{len(no_illustration)} cards without illustration_id")
    if n_illust_frames > n_illustrations:
        extra = n_illust_frames - n_illustrations
        print(f"[dedup] +{extra} extra images from multi-frame reprints "
              f"(same art, different frame -> different phash)")

    representative_cards = []
    printings_map = {}

    # Score each card to pick the best representative within a bucket:
    # prefer English, paper, non-excluded set, non-promo
    def score(c):
        s = 0
        if c.get('lang') == 'en':
            s += 100
        if 'paper' in c.get('games', []):
            s += 50
        if c.get('set', '').lower() not in EXCLUDED_SETS:
            s += 25
        if not c.get('promo', False):
            s += 10
        return s

    for (illust_id, frame), cards in by_illust_frame.items():
        cards.sort(key=score, reverse=True)
        rep = cards[0]
        rep_id = rep['id']

        representative_cards.append(rep)

        # Back-face variants are downloadable and hashable, but they must
        # not own a printings_map entry. At match time a back-face hit is
        # rewritten to the real card_id (via the hash DB canonical_id
        # field), and the real card already has its own printings entry
        # from its face-0 row.
        if _is_back_face_id(rep_id):
            continue

        # Build printings list from ALL cards sharing this art (across
        # frames). The web UI shows every possible printing a user's scan
        # could correspond to, regardless of which frame bucket won.
        all_art_cards = by_illust.get(illust_id, cards)
        printings = []
        for c in all_art_cards:
            printings.append({
                "id": c.get('id'),
                "set": c.get('set', ''),
                "set_name": c.get('set_name', ''),
                "collector_number": str(c.get('collector_number', '')),
                "lang": c.get('lang', ''),
                "frame": c.get('frame', ''),
            })

        printings_map[rep_id] = {
            "name": rep.get('name', 'Unknown'),
            "illustration_id": illust_id,
            "frame": frame,
            "printings": printings,
        }

    # Cards without illustration_id get their own entries (no dedup possible)
    for card in no_illustration:
        if 'image_uris' in card:
            card_id = card['id']
            representative_cards.append(card)
            # Same back-face rule as above.
            if _is_back_face_id(card_id):
                continue
            printings_map[card_id] = {
                "name": card.get('name', 'Unknown'),
                "illustration_id": None,
                "frame": card.get('frame', ''),
                "printings": [{
                    "id": card_id,
                    "set": card.get('set', ''),
                    "set_name": card.get('set_name', ''),
                    "collector_number": str(card.get('collector_number', '')),
                    "lang": card.get('lang', ''),
                    "frame": card.get('frame', ''),
                }],
            }

    print(f"[dedup] {len(representative_cards)} images to download "
          f"(saved {len(all_cards) - len(representative_cards)} duplicates)")

    return representative_cards, printings_map


def download_card_images(cards, output_dir, image_size='png', calls_per_second=10):
    """
    Downloads card images from the Scryfall card data provided in the `cards` list,
    with a rate limit on calls per second.
    """
    os.makedirs(output_dir, exist_ok=True)
    delay = 1.0 / calls_per_second

    total_cards = len(cards)
    skipped = 0
    downloaded = 0
    errors = 0

    # Import exclusion filter (shared with build_hash_db_v3.py)
    try:
        from build_hash_db_v3 import should_exclude_card
    except ImportError:
        def should_exclude_card(c):
            return False

    for idx, card in enumerate(cards, start=1):
        card_name = card.get('name', 'Unknown')
        card_id = card.get('id', card_name.replace(' ', '_'))

        # Skip non-gameplay cards (oversized, art series, bios, etc.)
        if should_exclude_card(card):
            skipped += 1
            continue

        # Check for image_uris
        if 'image_uris' not in card:
            skipped += 1
            continue

        image_uri = card['image_uris'].get(image_size)
        if not image_uri:
            skipped += 1
            continue

        filename = f"{card_id}.png"
        output_path = os.path.join(output_dir, filename)

        if os.path.exists(output_path):
            skipped += 1
            continue

        # Print progress every 500 cards
        if idx % 500 == 0:
            print(f"  Progress: {idx}/{total_cards} "
                  f"(downloaded={downloaded}, skipped={skipped}, errors={errors})")

        try:
            response = requests.get(image_uri, stream=True)
            response.raise_for_status()
            with open(output_path, 'wb') as img_file:
                for chunk in response.iter_content(chunk_size=8192):
                    img_file.write(chunk)
            downloaded += 1
        except Exception as e:
            print(f"  [ERROR] Failed to download {card_name}: {e}")
            errors += 1
            continue

        time.sleep(delay)

    print(f"\nDone: {downloaded} downloaded, {skipped} skipped, {errors} errors "
          f"(out of {total_cards} total)")


if __name__ == "__main__":
    # Step 1: Download latest Scryfall bulk data
    json_path = download_scryfall_bulk_data()
    if not json_path:
        # Fall back to newest existing file
        existing = sorted(glob.glob(os.path.join(SCRIPT_DIR, "default-cards-*.json")))
        if existing:
            json_path = existing[-1]
            print(f"[fallback] Using existing file: {json_path}")
        else:
            print("[ERROR] No Scryfall bulk data available. Exiting.")
            exit(1)

    # Step 2: Load card data
    print(f"Loading card data from: {json_path}")
    with open(json_path, 'r', encoding='utf-8') as f:
        all_cards = json.load(f)
    print(f"Found {len(all_cards)} cards in the JSON file.")

    # Step 3: Deduplicate by illustration_id and build printings map
    representative_cards, printings_map = build_printings_map(all_cards)

    # Save printings map
    printings_map_path = os.path.join(SCRIPT_DIR, "printings_map.json")
    with open(printings_map_path, 'w', encoding='utf-8') as f:
        json.dump(printings_map, f, ensure_ascii=False, indent=2)
    print(f"[dedup] Saved printings map to {printings_map_path} "
          f"({len(printings_map)} entries)")

    # Step 4: Download images (only unique art)
    output_dir = os.path.join(SCRIPT_DIR, "downloaded_cards")
    image_size = 'png'
    calls_per_second = 10

    sets_input = input("Enter comma-separated set codes to download first "
                       "(or leave blank for all): ").strip()

    if sets_input:
        desired_sets = [s.strip().lower() for s in sets_input.split(',')]
        priority_cards = [c for c in representative_cards
                          if c.get('set', '').lower() in desired_sets]
        remaining_cards = [c for c in representative_cards
                           if c.get('set', '').lower() not in desired_sets]

        if priority_cards:
            print(f"\nDownloading priority sets: {', '.join(desired_sets)} "
                  f"({len(priority_cards)} cards)")
            download_card_images(priority_cards, output_dir, image_size, calls_per_second)

        print(f"\nDownloading remaining cards ({len(remaining_cards)} cards)...")
        download_card_images(remaining_cards, output_dir, image_size, calls_per_second)
    else:
        download_card_images(representative_cards, output_dir, image_size, calls_per_second)

    # Step 5: Print the JSON filename for config update
    json_filename = os.path.basename(json_path)
    print(f"\n{'='*60}")
    print(f"Bulk data file: {json_filename}")
    print(f"Images downloaded (unique art only): {len(representative_cards)}")
    print(f"Printings map: printings_map.json ({len(printings_map)} entries)")
    print(f"\nNext steps:")
    print(f"  1. python 16bit_rgb_create_card_hashes.py")
    print(f"  2. Update CARDS_JSON_PATH in config.py to: {json_filename}")
    print(f"{'='*60}")
